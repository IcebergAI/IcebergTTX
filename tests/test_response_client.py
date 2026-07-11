"""Participant response form semantics, exercised against the shipped JavaScript."""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_participant_client_enforces_response_matrix_and_submits_option_only_payload():
    if not shutil.which("node"):
        pytest.skip("Node.js is required to exercise the participant response component")

    script = textwrap.dedent(
        """
        const assert = require('node:assert/strict');
        const callbacks = [];
        const components = {};
        const requests = [];

        global.document = {
          addEventListener(name, callback) {
            if (name === 'alpine:init') callbacks.push(callback);
          },
        };
        global.Alpine = {
          data(name, factory) { components[name] = factory; },
        };
        global.DT = { uiHelpers: {} };
        global.apiFetch = async (url, options) => {
          requests.push({ url, body: JSON.parse(options.body) });
          return { ok: true, status: 201 };
        };
        global.readJson = async () => ({});

        require('./static/js/pages/exercises.js');
        callbacks.forEach(callback => callback());

        (async () => {
          const view = components.participantView(42);
          view.exercise = { state: 'active' };

          const combined = {
            id: 1,
            _options: [{ id: 'approve', label: 'Approve' }],
            free_text_response: true,
          };
          assert.equal(view.canSubmit(combined), false);
          assert.equal(view.responseValidationMessage(combined), 'Select a decision to continue.');
          view.selectedOption[1] = 'stale-option';
          assert.equal(view.canSubmit(combined), false);
          view.selectedOption[1] = 'approve';
          assert.equal(
            view.responseValidationMessage(combined),
            'Explain the reasoning for your decision.',
          );
          view.freeText[1] = 'This contains the incident.';
          assert.equal(view.canSubmit(combined), true);

          const optionOnly = {
            id: 2,
            _options: [{ id: 'ack', label: 'Acknowledge' }],
            free_text_response: false,
          };
          assert.equal(view.requiresFreeText(optionOnly), false);
          assert.equal(view.canSubmit(optionOnly), false);
          view.selectedOption[2] = 'ack';
          assert.equal(view.canSubmit(optionOnly), true);
          await view.submitResponse(optionOnly);
          assert.deepEqual(requests[0], {
            url: '/exercises/42/responses',
            body: { inject_id: 2, content: '', selected_option: 'ack' },
          });

          const linear = { id: 3, _options: [], free_text_response: false };
          assert.equal(view.requiresFreeText(linear), true);
          assert.equal(view.canSubmit(linear), false);
          view.freeText[3] = 'We will isolate the host.';
          assert.equal(view.canSubmit(linear), true);

          view.exercise.state = 'paused';
          assert.equal(view.canSubmit(linear), false);
        })().catch(error => {
          console.error(error);
          process.exitCode = 1;
        });
        """
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
