from __future__ import annotations

import ast
import re
from pathlib import Path
from string import Formatter

import pytest

from agent_telegram import distribution
from agent_telegram import i18n
from agent_telegram.locales.zh import MESSAGES

ROOT = Path(__file__).resolve().parents[1] / 'src' / 'agent_telegram'
PENDING = set()
EXCLUDED = {'tgbridge/hook_report.py'}  # Its stdlib-only import closure is intentional.
SHARED = (
    'codex_app_client.py', 'codex_threads.py', 'codex_watch.py', 'codex_send.py',
    'jobs.py', 'worker.py', 'outputs.py', 'gate.py',
    'delegation.py', 'i18n.py', 'distribution.py',
)
CJK = re.compile(r'[\u3400-\u9fff]')


def converted_sources():
    paths = {p.relative_to(ROOT).as_posix() for p in (ROOT / 'tgbridge').glob('*.py')} | set(SHARED)
    for relative in sorted(paths - PENDING - EXCLUDED):
        source = (ROOT / relative).read_text(encoding='utf-8')
        yield relative, source


@pytest.mark.parametrize(('value', 'expected'), [
    ('en', 'en'), ('ENGLISH', 'en'), ('zh', 'zh'), ('CN', 'zh'),
    ('zh-CN', 'zh'), ('ZH_cn', 'zh'), ('中文', 'zh'),
    ('', None), ('fr', None), (None, None), (123, None),
])
def test_normalize(value, expected):
    assert i18n.normalize(value) == expected


def test_set_use_and_restore_on_exception():
    original = i18n.current()
    try:
        i18n.set_language('english')
        assert i18n.current() == 'en'
        with pytest.raises(RuntimeError), i18n.use('zh'):
            assert i18n.current() == 'zh'
            with i18n.use('en'):
                assert i18n.current() == 'en'
            raise RuntimeError('test')
        assert i18n.current() == 'en'
        with pytest.raises(ValueError):
            i18n.set_language('invalid')
        assert i18n.current() == 'en'
    finally:
        i18n.set_language(original)


def test_translation_fallback_and_optional_formatting():
    with i18n.use('en'):
        assert i18n.tr('Sent.') == 'Sent.'
        assert i18n.tr('Unknown key {name}') == 'Unknown key {name}'
        assert i18n.tr('Unknown key {name}', name='x') == 'Unknown key x'
    with i18n.use('zh'):
        assert i18n.tr('Sent.') == '已发送。'
        assert i18n.tr('Unknown key {name}') == 'Unknown key {name}'
        assert i18n.tr('Language saved: {language}.', language='zh') == '语言已保存：zh。'
    assert i18n.LANGUAGES == ('en', 'zh')
    assert distribution.DEFAULT_LANGUAGE == 'en'


def placeholders(text):
    found = set()
    for _, field, spec, _ in Formatter().parse(text):
        if field is not None:
            found.add(field)
        if spec:
            found.update(placeholders(spec))
    return found


def test_literal_calls_complete_table_and_matching_placeholders():
    used = set()
    for relative, source in converted_sources():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr':
                assert node.args and isinstance(node.args[0], ast.Constant), (relative, node.lineno)
                key = node.args[0].value
                assert isinstance(key, str), (relative, node.lineno)
                assert key in MESSAGES, (relative, node.lineno, key)
                assert not CJK.search(key), (relative, node.lineno, key)
                used.add(key)
    assert set(MESSAGES) == used, {'unused': set(MESSAGES) - used}
    for key, value in MESSAGES.items():
        assert placeholders(key) == placeholders(value), (key, value)


def test_no_unmarked_cjk_in_converted_modules():
    assert PENDING <= {'tgbridge/bridge.py'}
    failures = []
    for relative, source in converted_sources():
        data = False
        for number, line in enumerate(source.splitlines(), 1):
            if '# i18n: data-begin' in line:
                assert not data, (relative, number, 'nested data block')
                data = True
            if CJK.search(line) and not data and '# i18n: data' not in line:
                failures.append(f'{relative}:{number}: {line}')
            if '# i18n: data-end' in line:
                assert data, (relative, number, 'unmatched data end')
                data = False
        assert not data, (relative, 'unclosed data block')
    assert not failures, '\n'.join(failures)


def test_no_translation_is_evaluated_at_import_time():
    class ImportTimeCalls(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            # Defaults/decorators execute at definition time; bodies do not.
            for value in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
                if value is not None:
                    self.visit(value)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            assert not (isinstance(node.func, ast.Name) and node.func.id == 'tr'), (relative, node.lineno)
            self.generic_visit(node)

    for relative, source in converted_sources():
        ImportTimeCalls().visit(ast.parse(source))
