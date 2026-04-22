from app.services.captcha_debug_analyzer import CaptchaDebugAnalyzer


def test_analyzer_detects_p1_token_and_empty_context():
    result = CaptchaDebugAnalyzer.analyze_solution(
        {
            'token': 'P1_abc',
            'raw': {
                'contextId': '',
                'generated_pass_UUID': 'P1_abc',
                'ua': 'ua',
                'lang': 'en-US',
            },
        }
    )

    assert result['token_is_hcaptcha_enterprise'] is True
    assert result['context_id_empty'] is True
    assert result['token_matches_generated_pass_uuid'] is True
    assert 'empty_context_id' in result['issues']


def test_analyzer_detects_token_mismatch():
    result = CaptchaDebugAnalyzer.analyze_solution(
        {
            'token': 'P1_token',
            'raw': {
                'contextId': 'ctx',
                'generated_pass_UUID': 'P1_other',
            },
        }
    )

    assert result['context_id'] == 'ctx'
    assert result['token_matches_generated_pass_uuid'] is False
    assert 'token_generated_pass_uuid_mismatch' in result['issues']
