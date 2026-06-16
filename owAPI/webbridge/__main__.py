try:
    from .main import main as _main
except Exception:
    import runpy, pathlib
    runpy.run_path(str(pathlib.Path(__file__).with_name("main.py")))
else:
    if __name__ == "__main__":
        _main()
