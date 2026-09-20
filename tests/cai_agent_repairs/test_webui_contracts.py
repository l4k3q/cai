from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WEBUI_APP = ROOT / "webui" / "js" / "app.js"
WEBUI_CSS = ROOT / "webui" / "css" / "style.css"
WEBUI_HTML = ROOT / "webui" / "index.html"


def _app_source() -> str:
    return WEBUI_APP.read_text(encoding="utf-8")


def _css_source() -> str:
    return WEBUI_CSS.read_text(encoding="utf-8")


def test_webui_base_url_defaults_are_canonical():
    app_source = _app_source()
    html_source = WEBUI_HTML.read_text(encoding="utf-8")

    assert html_source.count('placeholder="https://api.deepseek.com"') == 4
    assert "api.mnapi.com" not in html_source
    assert "your-gateway/v1" not in html_source
    assert "const DEFAULT_BASE_URL = 'https://api.deepseek.com';" in app_source
    assert "els.inpBaseUrl.value = DEFAULT_BASE_URL;" in app_source
    assert "els.bankBaseUrl.value = DEFAULT_BASE_URL;" in app_source
    assert "els.qbankBaseUrl.value = DEFAULT_BASE_URL;" in app_source
    assert ": DEFAULT_BASE_URL;" in app_source


def test_regular_webui_messages_are_always_chat_messages():
    source = _app_source()

    assert "content_kind: 'chat'" in source
    assert "content_kind: state.sessionResolved ? 'chat' : 'question_statement'" not in source


def test_spectator_polling_survives_slow_evaluation_and_session_reload():
    source = _app_source()

    assert "const SPECTATOR_POLL_MAX = 30;" in source
    select_session = source[source.index("async function selectSession") : source.index("function clearChat")]
    assert "spectatorStartPolling();" in select_session


def test_spectator_accept_keeps_the_existing_binding_and_teaching_flow():
    source = _app_source()

    assert "/spectator/card/accept" in source
    assert "body: JSON.stringify({ card_id: card.card_id, question_id: questionId })" in source
    assert "sendMessage(data.teaching_prompt_hint || '开始学习这道题')" in source


def test_spectator_failure_card_supports_retry_and_abandon():
    source = _app_source()
    css = _css_source()
    html = WEBUI_HTML.read_text(encoding="utf-8")

    assert "card.kind === 'failure'" in source
    assert "error_message" in source
    assert "retryable" in source
    assert "/spectator/card/retry" in source
    assert "/spectator/card/abandon" in source
    assert "旁观 Agent 连接失败" in source
    assert "重新连接" in source
    assert "放弃" in source
    assert "spectatorRenderCard(nextCard);" in source
    assert ".spectator-failure" in css
    assert 'aria-live="polite"' in html


def test_backend_keeps_explicit_question_statement_support():
    backend_files = [
        ROOT / "src" / "cai" / "api" / "app.py",
        ROOT / "cai-ops" / "new_send_message_stream.py",
    ]
    existing_files = [path for path in backend_files if path.is_file()]

    assert existing_files
    assert any(
        'payload.content_kind == "question_statement"' in path.read_text(encoding="utf-8")
        for path in existing_files
    )


def test_deepseek_default_examples_use_the_canonical_base_url():
    candidate_files = [
        ROOT / "DEVELOPMENT.md",
        ROOT / "benchmarks" / "eval.py",
        ROOT / ".env",
    ]
    existing_files = [path for path in candidate_files if path.is_file()]

    assert existing_files
    for path in existing_files:
        source = path.read_text(encoding="utf-8")
        assert "https://api.deepseek.com" in source
        assert "https://api.deepseek.com/v1" not in source


def test_handoff_marks_the_old_gateway_as_historical():
    handoff = ROOT / "HANDOFF.md"
    if not handoff.is_file():
        return

    source = handoff.read_text(encoding="utf-8")
    assert 'DEEPSEEK_API_BASE="https://api.deepseek.com"' in source
    assert "tokenrhythm.studio/v1" not in source
    assert "[历史故障]" in source


def test_operational_examples_use_the_canonical_deepseek_url():
    candidate_files = [ROOT / "test_fetch.sh", ROOT / "开发流程梳理.md"]

    for path in candidate_files:
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        assert "tokenrhythm.studio/v1" not in source
        assert "https://api.deepseek.com" in source
