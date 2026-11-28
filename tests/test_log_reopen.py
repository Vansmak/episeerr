"""Log files deleted while the app is running get recreated (issue #111)."""
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from log_handlers import SelfHealingFileHandler, SelfHealingRotatingFileHandler  # noqa: E402


def _logger(handler, name):
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter('%(message)s'))
    lg.addHandler(handler)
    return lg


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _check(handler_cls, name, **kwargs):
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'logs', 'x.log')
    os.makedirs(os.path.dirname(path))
    h = handler_cls(path, encoding='utf-8', **kwargs)
    lg = _logger(h, name)
    try:
        lg.info('first')
        assert 'first' in _read(path)

        # Delete the file: next write recreates it.
        os.remove(path)
        lg.info('second')
        assert os.path.exists(path)
        assert _read(path).strip() == 'second'

        # Delete the whole folder: next write recreates folder and file.
        shutil.rmtree(os.path.dirname(path))
        lg.info('third')
        assert _read(path).strip() == 'third'
    finally:
        h.close()
        shutil.rmtree(d, ignore_errors=True)


def test_rotating_handler_recreates_deleted_file():
    _check(SelfHealingRotatingFileHandler, 'reopen_rot', maxBytes=1024 * 1024, backupCount=1)


def test_plain_handler_recreates_deleted_file():
    _check(SelfHealingFileHandler, 'reopen_plain')


def test_rotation_still_works():
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'r.log')
    h = SelfHealingRotatingFileHandler(path, maxBytes=50, backupCount=2, encoding='utf-8')
    lg = _logger(h, 'reopen_rotation')
    try:
        for i in range(20):
            lg.info('line %02d xxxxxxxxxx', i)
        assert os.path.exists(path + '.1')
        assert 'line 19' in _read(path)
    finally:
        h.close()
        shutil.rmtree(d, ignore_errors=True)
