import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace


class FakeSession:
    def __init__(self):
        self.active = False

    def __enter__(self):
        self.active = True
        return self

    def __exit__(self, *_args):
        self.active = False


def load_file_watcher(monkeypatch):
    processed = []

    class FakeProcessor:
        def __init__(self, db):
            assert db.active
            self.db = db

        def process_file(self, path, db):
            assert db is self.db
            assert db.active
            processed.append(path)
            return object()

    processor_module = types.ModuleType("app.services.document_processor")
    processor_module.DocumentProcessor = FakeProcessor
    monkeypatch.setitem(sys.modules, "app.services.document_processor", processor_module)

    sys.modules.pop("app.services.file_watcher", None)
    module = importlib.import_module("app.services.file_watcher")
    monkeypatch.setattr(module, "SessionLocal", FakeSession)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module, processed


def test_created_file_uses_a_live_database_session(tmp_path, monkeypatch):
    module, processed = load_file_watcher(monkeypatch)
    uploaded_file = tmp_path / "invoice.pdf"
    uploaded_file.write_bytes(b"document")
    settings = SimpleNamespace(allowed_extensions_list=["pdf"])
    handler = module.FileWatcherHandler(settings)

    handler.on_created(SimpleNamespace(is_directory=False, src_path=str(uploaded_file)))

    assert processed == [uploaded_file]


def test_start_recovers_files_that_precede_the_observer(tmp_path, monkeypatch):
    module, _processed = load_file_watcher(monkeypatch)

    class FakeObserver:
        def schedule(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

    watcher = module.FileWatcher()
    watcher.observer = FakeObserver()
    watcher.settings = SimpleNamespace(staging_folder=str(tmp_path), allowed_extensions_list=["pdf"])
    watcher._initialized = True
    recovered = []
    monkeypatch.setattr(watcher, "_process_existing_files", lambda: recovered.append(True))

    watcher.start()

    assert watcher.is_running is True
    assert recovered == [True]
