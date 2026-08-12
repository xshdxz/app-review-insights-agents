import importlib.util
from pathlib import Path


def test_streamlit_entrypoint_is_importable():
    app_path = Path(__file__).parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("app_entry", app_path)

    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.main)
