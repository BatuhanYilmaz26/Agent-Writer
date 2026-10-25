"""Keep saved browser preferences when upgrading the workspace name."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_preferences_migrate_once_and_retain_old_value_after_failed_write():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise the browser preference module")
    module = Path(__file__).resolve().parents[2] / "src/fieldnote/web/preferences.js"
    script = "import { migratePreferences } from " + json.dumps(module.as_uri()) + ";\n" + """
import assert from 'node:assert/strict';
const values = new Map([['agentwriter.theme', 'dark'], ['agentwriter.run', 'saved-run']]);
const storage = {getItem: (k) => values.get(k) ?? null,
  setItem: (k, v) => values.set(k, v), removeItem: (k) => values.delete(k)};
migratePreferences(storage);
assert.equal(values.get('fieldnote.theme'), 'dark');
assert.equal(values.get('fieldnote.run'), 'saved-run');
assert.equal(values.has('agentwriter.run'), false);
values.set('agentwriter.theme', 'dark'); values.set('fieldnote.theme', 'light');
migratePreferences(storage);
assert.equal(values.get('fieldnote.theme'), 'light');
assert.equal(values.has('agentwriter.theme'), false);
migratePreferences(storage);
assert.equal(values.get('fieldnote.run'), 'saved-run');
values.set('agentwriter.run', 'recoverable-run'); values.delete('fieldnote.run');
assert.throws(() => migratePreferences({...storage, setItem: () => { throw Error('quota'); }}));
assert.equal(values.get('agentwriter.run'), 'recoverable-run');
migratePreferences(storage);
assert.equal(values.get('fieldnote.run'), 'recoverable-run');
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
