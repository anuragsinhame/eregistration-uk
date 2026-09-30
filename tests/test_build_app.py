import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_app  # noqa: E402


def test_prepare_dmg_contents_creates_applications_alias():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        app = root / "UK e-Registration Search.app"
        app.mkdir()

        stage = build_app.prepare_dmg_contents(app, root / "dmg")

        assert stage.is_dir()
        assert (stage / "UK e-Registration Search.app").is_dir()
        assert (stage / "Applications").is_symlink()
        assert (stage / "Applications").resolve() == Path("/Applications")

        print("build_app: DMG layout test passed")


if __name__ == "__main__":
    test_prepare_dmg_contents_creates_applications_alias()
