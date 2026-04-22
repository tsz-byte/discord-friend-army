from __future__ import annotations


class CaptchaDebugAnalyzer:
    """Inspect AnySolver captcha solutions for Discord compatibility debugging."""

    @staticmethod
    def analyze_solution(solution: dict | None) -> dict:
        solution = solution if isinstance(solution, dict) else {}
        raw = solution.get('raw')
        raw = raw if isinstance(raw, dict) else {}

        token = (
            str(solution.get('token') or '')
            or str(solution.get('gRecaptchaResponse') or '')
            or str(raw.get('generated_pass_UUID') or '')
        )
        generated_pass_uuid = str(raw.get('generated_pass_UUID') or '')
        context_id_value = raw.get('contextId')
        context_id = '' if context_id_value is None else str(context_id_value)
        token_prefix = token.split('_', 1)[0] if '_' in token else ''
        token_is_hcaptcha_enterprise = token.startswith('P1_')
        context_id_empty = context_id == ''
        token_matches_generated_pass_uuid = (
            bool(token) and bool(generated_pass_uuid) and token == generated_pass_uuid
        )

        issues: list[str] = []
        if not token:
            issues.append('missing_token')
        if context_id_empty:
            issues.append('empty_context_id')
        if generated_pass_uuid and not token_matches_generated_pass_uuid:
            issues.append('token_generated_pass_uuid_mismatch')
        if token and not token_is_hcaptcha_enterprise:
            issues.append('non_p1_token_prefix')

        return {
            'token': token,
            'raw': raw,
            'ua': str(raw.get('ua') or ''),
            'lang': str(raw.get('lang') or ''),
            'context_id': context_id,
            'context_id_empty': context_id_empty,
            'generated_pass_uuid': generated_pass_uuid,
            'token_prefix': token_prefix,
            'token_is_hcaptcha_enterprise': token_is_hcaptcha_enterprise,
            'token_matches_generated_pass_uuid': token_matches_generated_pass_uuid,
            'issues': issues,
        }
