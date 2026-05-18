//! API request/response models for `DeepSeek` and OpenAI-compatible endpoints.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{OnceLock, RwLock};

/// One row from the OwlTrail adapter's `/v1/models` endpoint.
#[derive(Debug, Deserialize, Clone, PartialEq, Eq)]
pub struct OwltrailModel {
    pub id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub context_window: Option<u32>,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub triage_available: bool,
}

impl OwltrailModel {
    #[must_use]
    pub fn is_available(&self) -> bool {
        self.status.eq_ignore_ascii_case("OK") || self.triage_available
    }

    #[must_use]
    pub fn display_label(&self) -> String {
        if self.name.trim().is_empty() {
            self.id.clone()
        } else {
            self.name.clone()
        }
    }
}

#[derive(Debug, Deserialize)]
struct OwltrailModelsResponse {
    #[serde(default)]
    data: Vec<OwltrailModel>,
}

/// Process-wide registry of dynamic per-model context windows reported by
/// the adapter at `/v1/models`. Consulted first by [`context_window_for_model`]
/// so a runtime fetch can override the static fallback table without
/// threading new state through every call site.
static DYNAMIC_CONTEXT_WINDOWS: OnceLock<RwLock<HashMap<String, u32>>> = OnceLock::new();

fn dynamic_context_windows() -> &'static RwLock<HashMap<String, u32>> {
    DYNAMIC_CONTEXT_WINDOWS.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Process-wide registry of friendly display names from the adapter (`name` field).
/// Used by [`display_model_name`] so non-DeepSeek models show their human
/// label ("Qwen3 Max") instead of their raw id ("alibaba-qwen3-max").
static DYNAMIC_MODEL_NAMES: OnceLock<RwLock<HashMap<String, String>>> = OnceLock::new();

fn dynamic_model_names() -> &'static RwLock<HashMap<String, String>> {
    DYNAMIC_MODEL_NAMES.get_or_init(|| RwLock::new(HashMap::new()))
}

fn dynamic_display_name(model: &str) -> Option<String> {
    dynamic_model_names()
        .read()
        .ok()
        .and_then(|r| r.get(model).cloned())
}

/// Replace the dynamic context-window registry with the supplied models'
/// `context_window` fields. Models without a context window are skipped so
/// the static fallback (`context_window_for_model`) still applies to them.
pub fn register_dynamic_models(models: &[OwltrailModel]) {
    let mut cw_map = HashMap::new();
    let mut name_map = HashMap::new();
    for m in models {
        if let Some(cw) = m.context_window {
            cw_map.insert(m.id.clone(), cw);
        }
        if !m.name.trim().is_empty() {
            let display = m.name.trim().to_string();
            // Full ID: "deepseek-deepseek-v4-pro" → "DeepSeek V4 Pro (Thinking)"
            name_map.insert(m.id.clone(), display.clone());
            // Also register short form: "deepseek-v4-pro" → same name
            // so display_model_name("deepseek-v4-pro") resolves without
            // the redundant owner prefix.
            if let Some(rest) = m.id.strip_prefix("deepseek-deepseek-") {
                name_map.entry(format!("deepseek-{rest}")).or_insert(display);
            }
        }
    }
    if let Ok(mut w) = dynamic_context_windows().write() {
        *w = cw_map;
    }
    if let Ok(mut w) = dynamic_model_names().write() {
        *w = name_map;
    }
}

fn dynamic_context_window(model: &str) -> Option<u32> {
    dynamic_context_windows()
        .read()
        .ok()
        .and_then(|r| r.get(model).copied())
}

/// Force a context-window value for `model` in the dynamic registry. Used by
/// the CLI `--context` override so the rest of the TUI (sidebar, capacity
/// flow, compaction trigger) reads the user-provided cap.
pub fn set_context_window_override(model: &str, tokens: u32) {
    if let Ok(mut w) = dynamic_context_windows().write() {
        w.insert(model.to_string(), tokens);
    }
}

/// Fetch `/v1/models` from the adapter and parse the response.
///
/// `base_url` should already point at the OpenAI-compatible root (e.g.
/// `http://127.0.0.1:8081/v1`). Times out fast so a missing adapter never
/// stalls TUI startup. Errors are returned, callers decide whether to fall
/// back to the hard-coded picker entries.
pub fn fetch_owltrail_models_blocking(base_url: &str) -> Result<Vec<OwltrailModel>, String> {
    let trimmed = base_url.trim_end_matches('/');
    let url = format!("{trimmed}/models");
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_millis(1500))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(&url)
        .send()
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| e.to_string())?;
    let parsed: OwltrailModelsResponse = resp.json().map_err(|e| e.to_string())?;
    Ok(parsed.data)
}

/// Context window used only for legacy DeepSeek model IDs that do not name a
/// newer V4 alias and do not carry an explicit `*k` suffix.
pub const LEGACY_DEEPSEEK_CONTEXT_WINDOW_TOKENS: u32 = 128_000;
pub const DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS: u32 = 1_000_000;
/// Last-resort compaction trigger when [`context_window_for_model`] returns
/// `None` (an unrecognised model id). v0.8.11 raised this from `50_000` to
/// `102_400` (80% of [`LEGACY_DEEPSEEK_CONTEXT_WINDOW_TOKENS`]) so unknown
/// models inherit the same late-trigger discipline as V4 instead of paying
/// the prefix-cache hit at 5% of the V4 window. Known DeepSeek / Claude
/// models resolve to their own scaled value via
/// [`compaction_threshold_for_model`] (#664).
pub const DEFAULT_COMPACTION_TOKEN_THRESHOLD: usize = 102_400;
const COMPACTION_THRESHOLD_PERCENT: u32 = 80;

// === Core Message Types ===

/// Request payload for sending a message to the API.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct MessageRequest {
    pub model: String,
    pub messages: Vec<Message>,
    pub max_tokens: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub system: Option<SystemPrompt>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<Tool>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_choice: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thinking: Option<serde_json::Value>,
    /// DeepSeek reasoning-effort tier: "off" | "low" | "medium" | "high" | "max".
    /// Translated by the client into DeepSeek's `reasoning_effort` + `thinking` fields.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reasoning_effort: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stream: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub top_p: Option<f32>,
}

/// System prompt representation (plain text or structured blocks).
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
#[serde(untagged)]
pub enum SystemPrompt {
    Text(String),
    Blocks(Vec<SystemBlock>),
}

/// A structured system prompt block.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct SystemBlock {
    #[serde(rename = "type")]
    pub block_type: String,
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cache_control: Option<CacheControl>,
}

/// A chat message with role and content blocks.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct Message {
    pub role: String,
    pub content: Vec<ContentBlock>,
}

/// A single content block inside a message.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
#[serde(tag = "type")]
pub enum ContentBlock {
    #[serde(rename = "text")]
    Text {
        text: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        cache_control: Option<CacheControl>,
    },
    #[serde(rename = "thinking")]
    Thinking { thinking: String },
    #[serde(rename = "tool_use")]
    ToolUse {
        id: String,
        name: String,
        input: serde_json::Value,
        #[serde(skip_serializing_if = "Option::is_none")]
        caller: Option<ToolCaller>,
    },
    #[serde(rename = "tool_result")]
    ToolResult {
        tool_use_id: String,
        content: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        is_error: Option<bool>,
        #[serde(skip_serializing_if = "Option::is_none")]
        content_blocks: Option<Vec<serde_json::Value>>,
    },
    #[serde(rename = "server_tool_use")]
    ServerToolUse {
        id: String,
        name: String,
        input: serde_json::Value,
    },
    #[serde(rename = "tool_search_tool_result")]
    ToolSearchToolResult {
        tool_use_id: String,
        content: serde_json::Value,
    },
    #[serde(rename = "code_execution_tool_result")]
    CodeExecutionToolResult {
        tool_use_id: String,
        content: serde_json::Value,
    },
}

/// Cache control metadata for tool definitions and blocks.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct CacheControl {
    #[serde(rename = "type")]
    pub cache_type: String,
}

/// Metadata describing who invoked a tool call.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct ToolCaller {
    #[serde(rename = "type")]
    pub caller_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_id: Option<String>,
}

/// Tool definition exposed to the model.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct Tool {
    #[serde(rename = "type", skip_serializing_if = "Option::is_none")]
    pub tool_type: Option<String>,
    pub name: String,
    pub description: String,
    pub input_schema: serde_json::Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub allowed_callers: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub defer_loading: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub input_examples: Option<Vec<serde_json::Value>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub strict: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cache_control: Option<CacheControl>,
}

/// Container metadata for code-execution style server tools.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ContainerInfo {
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<String>,
}

/// Server-side tool usage counters.
#[derive(Debug, Serialize, Deserialize, Clone, Default, PartialEq, Eq)]
pub struct ServerToolUsage {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code_execution_requests: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_search_requests: Option<u32>,
}

/// Response payload for a message request.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct MessageResponse {
    pub id: String,
    pub r#type: String,
    pub role: String,
    pub content: Vec<ContentBlock>,
    pub model: String,
    pub stop_reason: Option<String>,
    pub stop_sequence: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub container: Option<ContainerInfo>,
    pub usage: Usage,
}

/// Token usage metadata for a response.
#[derive(Debug, Serialize, Deserialize, Clone, Default, PartialEq, Eq)]
pub struct Usage {
    pub input_tokens: u32,
    pub output_tokens: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prompt_cache_hit_tokens: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prompt_cache_miss_tokens: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reasoning_tokens: Option<u32>,
    /// Approximate input tokens spent re-sending prior `reasoning_content`
    /// across user-message boundaries in DeepSeek V4 thinking-mode tool-calling
    /// turns (V4 §5.1.1 "Interleaved Thinking"). Estimated client-side at
    /// ~4 chars/token from the outgoing request body, before the model sees it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reasoning_replay_tokens: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub server_tool_use: Option<ServerToolUsage>,
}

/// Map known models to their approximate context window sizes.
///
/// Consults the runtime-fetched registry (see [`register_dynamic_models`])
/// first; falls back to the static DeepSeek-family heuristics below.
#[must_use]
pub fn context_window_for_model(model: &str) -> Option<u32> {
    if let Some(win) = dynamic_context_window(model) {
        return Some(win);
    }
    let lower = model.to_lowercase();
    // Unknown legacy DeepSeek model IDs default to 128K unless an explicit
    // *k suffix is present. DeepSeek-V4 family and current compatibility
    // aliases ship with a 1M context window.
    if lower.contains("deepseek") {
        if let Some(explicit_window) = deepseek_context_window_hint(&lower) {
            return Some(explicit_window);
        }
        if lower.contains("v4") {
            return Some(DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS);
        }
        return Some(LEGACY_DEEPSEEK_CONTEXT_WINDOW_TOKENS);
    }
    if lower.contains("claude") {
        return Some(200_000);
    }
    None
}

fn deepseek_context_window_hint(model_lower: &str) -> Option<u32> {
    let bytes = model_lower.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i].is_ascii_digit() {
            let start = i;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                i += 1;
            }
            if i >= bytes.len() || bytes[i] != b'k' {
                continue;
            }

            let before_ok = start == 0 || !bytes[start - 1].is_ascii_alphanumeric();
            let after_ok = i + 1 >= bytes.len() || !bytes[i + 1].is_ascii_alphanumeric();
            if !before_ok || !after_ok {
                continue;
            }

            if let Ok(kilo_tokens) = model_lower[start..i].parse::<u32>()
                && (8..=1024).contains(&kilo_tokens)
            {
                return Some(kilo_tokens.saturating_mul(1000));
            }
        } else {
            i += 1;
        }
    }
    None
}

/// Derive a compaction token threshold from model context window.
///
/// Keeps headroom for tool outputs and assistant completion by defaulting to 80%
/// of known context windows.
#[must_use]
pub fn compaction_threshold_for_model(model: &str) -> usize {
    let Some(window) = context_window_for_model(model) else {
        return DEFAULT_COMPACTION_TOKEN_THRESHOLD;
    };

    let threshold = (u64::from(window) * u64::from(COMPACTION_THRESHOLD_PERCENT)) / 100;
    usize::try_from(threshold).unwrap_or(DEFAULT_COMPACTION_TOKEN_THRESHOLD)
}

/// Compaction threshold keyed by model and caller-supplied effort tier.
///
/// Replacement-style compaction rewrites the stable prefix, which works against
/// DeepSeek V4 prefix-cache economics. Reasoning effort must not lower V4's
/// automatic replacement threshold; V4-family models use the same late
/// 80%-of-window guard as `compaction_threshold_for_model`.
#[must_use]
pub fn compaction_threshold_for_model_and_effort(
    model: &str,
    _reasoning_effort: Option<&str>,
) -> usize {
    compaction_threshold_for_model(model)
}

// === Streaming Structures ===

#[allow(dead_code)]
#[derive(Debug, Deserialize, Clone)]
#[serde(tag = "type")]
/// Streaming event types for SSE responses.
pub enum StreamEvent {
    #[serde(rename = "message_start")]
    MessageStart { message: MessageResponse },
    #[serde(rename = "content_block_start")]
    ContentBlockStart {
        index: u32,
        content_block: ContentBlockStart,
    },
    #[serde(rename = "content_block_delta")]
    ContentBlockDelta { index: u32, delta: Delta },
    #[serde(rename = "content_block_stop")]
    ContentBlockStop { index: u32 },
    #[serde(rename = "message_delta")]
    MessageDelta {
        delta: MessageDelta,
        usage: Option<Usage>,
    },
    #[serde(rename = "message_stop")]
    MessageStop,
    #[serde(rename = "ping")]
    Ping,
}

#[allow(dead_code)]
#[derive(Debug, Deserialize, Clone)]
#[serde(tag = "type")]
/// Content block types used in streaming starts.
pub enum ContentBlockStart {
    #[serde(rename = "text")]
    Text { text: String },
    #[serde(rename = "thinking")]
    Thinking { thinking: String },
    #[serde(rename = "tool_use")]
    ToolUse {
        id: String,
        name: String,
        input: serde_json::Value, // usually empty or partial
        #[serde(skip_serializing_if = "Option::is_none")]
        caller: Option<ToolCaller>,
    },
    #[serde(rename = "server_tool_use")]
    ServerToolUse {
        id: String,
        name: String,
        input: serde_json::Value,
    },
}

// Variant names match legacy streaming spec, suppressing style warning
#[allow(clippy::enum_variant_names)]
#[derive(Debug, Deserialize, Clone)]
#[serde(tag = "type")]
/// Delta events emitted during streaming responses.
pub enum Delta {
    #[serde(rename = "text_delta")]
    TextDelta { text: String },
    #[serde(rename = "thinking_delta")]
    ThinkingDelta { thinking: String },
    #[serde(rename = "input_json_delta")]
    InputJsonDelta { partial_json: String },
}

#[allow(dead_code)]
#[derive(Debug, Deserialize, Clone)]
/// Delta payload for message-level updates.
pub struct MessageDelta {
    pub stop_reason: Option<String>,
    pub stop_sequence: Option<String>,
}

/// Map an internal model identifier to the user-facing display label.
/// Internal IDs (`deepseek-v4-pro`, …) are still used everywhere the value
/// is sent over the wire — this only changes what the user sees in the UI.
#[must_use]
pub fn display_model_name(model: &str) -> String {
    if let Some(rest) = model.strip_prefix("auto: ") {
        return format!("auto: {}", display_model_name(rest));
    }
    // The OwlTrail adapter advertises DeepSeek models with a redundant
    // `deepseek-` owner prefix (`deepseek-deepseek-v4-pro`). When that
    // form leaks into `app.model` (e.g. selected from `/models` or the
    // picker), drop the duplicate prefix before consulting the alias
    // table so it still maps to the friendly `owltrail-pro` label.
    // Normalize "deepseek-deepseek-v4-pro" → "deepseek-v4-pro" so both forms
    // resolve to the same dynamic-map entry (registered by register_dynamic_models).
    let lookup = model
        .strip_prefix("deepseek-deepseek-")
        .map(|rest| format!("deepseek-{rest}"))
        .unwrap_or_else(|| model.to_string());
    // Look up the friendly name from the adapter's /v1/models response.
    // Falls back to the raw id when the dynamic registry is empty (e.g.
    // the adapter wasn't reachable at startup).
    if let Some(name) = dynamic_display_name(&lookup) {
        return name;
    }
    // Also try without the owner prefix (e.g. "alibaba-qwen3-max" → "qwen3-max")
    if let Some((_owner, rest)) = lookup.split_once('-') {
        if let Some(name) = dynamic_display_name(rest) {
            return name;
        }
    }
    lookup
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_model_name_uses_raw_id_without_dynamic_map() {
        // When the dynamic map is empty (adapter not reachable), display_model_name
        // falls back to the normalised model ID string.
        assert_eq!(display_model_name("deepseek-v4-pro"), "deepseek-v4-pro");
        assert_eq!(display_model_name("deepseek-v4-flash"), "deepseek-v4-flash");
        assert_eq!(display_model_name("deepseek-v4-web"), "deepseek-v4-web");
        assert_eq!(display_model_name("deepseek-chat"), "deepseek-chat");
        assert_eq!(display_model_name("deepseek-reasoner"), "deepseek-reasoner");
        assert_eq!(display_model_name("custom-model"), "custom-model");
        assert_eq!(
            display_model_name("auto: deepseek-v4-pro"),
            "auto: deepseek-v4-pro"
        );
        // OwlTrail adapter advertises DeepSeek models with a redundant
        // `deepseek-` owner prefix — should be normalised to the short form.
        assert_eq!(
            display_model_name("deepseek-deepseek-v4-pro"),
            "deepseek-v4-pro"
        );
        assert_eq!(
            display_model_name("deepseek-deepseek-v4-flash"),
            "deepseek-v4-flash"
        );
    }

    #[test]
    fn v4_snapshots_preserve_context_window() {
        // v-series snapshots get 1M context since they contain "v4"
        assert_eq!(
            context_window_for_model("deepseek-v4-flash-20260423"),
            Some(DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS)
        );
        assert_eq!(
            context_window_for_model("deepseek-v4-pro-20260423"),
            Some(DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS)
        );
    }

    #[test]
    fn unknown_legacy_deepseek_models_map_to_128k_context_window() {
        assert_eq!(
            context_window_for_model("deepseek-coder"),
            Some(LEGACY_DEEPSEEK_CONTEXT_WINDOW_TOKENS)
        );
        assert_eq!(
            context_window_for_model("deepseek-v3.2-0324"),
            Some(LEGACY_DEEPSEEK_CONTEXT_WINDOW_TOKENS)
        );
    }

    #[test]
    fn deepseek_v4_models_map_to_1m_context_window() {
        assert_eq!(
            context_window_for_model("deepseek-v4-pro"),
            Some(DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS)
        );
        assert_eq!(
            context_window_for_model("deepseek-v4-flash"),
            Some(DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS)
        );
        assert_eq!(
            context_window_for_model("deepseek-ai/deepseek-v4-pro"),
            Some(DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS)
        );
    }

    #[test]
    fn deepseek_models_with_k_suffix_use_hint() {
        assert_eq!(context_window_for_model("deepseek-v3.2-32k"), Some(32_000));
        assert_eq!(
            context_window_for_model("deepseek-v3.2-256k-preview"),
            Some(256_000)
        );
        assert_eq!(
            context_window_for_model("deepseek-v3.2-2k-preview"),
            Some(LEGACY_DEEPSEEK_CONTEXT_WINDOW_TOKENS)
        );
    }

    #[test]
    fn compaction_threshold_scales_with_context_window() {
        assert_eq!(
            compaction_threshold_for_model("deepseek-v3.2-128k"),
            102_400
        );
        // v0.8.11 (#664): unknown-model fallback also resolves to 80% of
        // `LEGACY_DEEPSEEK_CONTEXT_WINDOW_TOKENS` (128K legacy DeepSeek
        // fallback) — same late-trigger discipline as the V4 path. Was
        // `50_000` pre-v0.8.11; that hardcoded value compacted at ~5% of a
        // 1M window when model detection silently fell through, which is
        // exactly the prefix-cache-burning behaviour we're getting away from.
        assert_eq!(compaction_threshold_for_model("unknown-model"), 102_400);
    }

    #[test]
    fn compaction_scales_for_deepseek_v4_1m_context() {
        assert_eq!(compaction_threshold_for_model("deepseek-v4-pro"), 800_000);
    }

    #[test]
    fn v4_replacement_compaction_ignores_reasoning_effort() {
        assert_eq!(
            compaction_threshold_for_model_and_effort("deepseek-v4-pro", Some("off")),
            800_000
        );
        assert_eq!(
            compaction_threshold_for_model_and_effort("deepseek-v4-pro", Some("high")),
            800_000
        );
        assert_eq!(
            compaction_threshold_for_model_and_effort("deepseek-v4-pro", Some("max")),
            800_000
        );
    }

    #[test]
    fn v4_soft_caps_only_apply_to_v4_models() {
        assert_eq!(
            compaction_threshold_for_model_and_effort("deepseek-v3.2-128k", Some("max")),
            102_400
        );
        // v0.8.11 (#664): unknown-model fallback also lands on the
        // 80%-of-128K legacy DeepSeek fallback instead of the legacy
        // hardcoded 50K, so model-detection-fall-through doesn't quietly
        // burn V4 prefix cache at 5%-of-window.
        assert_eq!(
            compaction_threshold_for_model_and_effort("unknown-model", Some("max")),
            102_400
        );
    }

    #[test]
    fn v4_replacement_compaction_defaults_to_late_guard_when_effort_unknown() {
        assert_eq!(
            compaction_threshold_for_model_and_effort("deepseek-v4-pro", None),
            800_000
        );
        assert_eq!(
            compaction_threshold_for_model_and_effort("deepseek-v4-pro", Some("unknown")),
            800_000
        );
    }
}
