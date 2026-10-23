"""LLM 호출 공용 조각 — 1호 추천 모듈(`recommender`)이 폐지되면서 AI 매도 판단이 쓰던 부분만 옮겼다."""

# 응답 토큰 한도. 사고(thinking) 토큰과 본문이 이 한도를 함께 쓰므로 넉넉히 잡는다.
# 부족하면 사고에 예산을 다 쓰고 본문이 비거나 잘려 파싱이 실패한다.
MAX_TOKENS = 16000


def extract_json(raw_text: str) -> str:
    """응답에서 JSON 본문만 꺼낸다.

    구조화 출력을 쓰면 본문은 순수 JSON이지만, 모델이나 프롬프트를 바꿨을 때
    마크다운 코드펜스(```json)나 앞뒤 설명이 붙어도 깨지지 않도록 방어한다.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()

    # 앞뒤에 산문이 섞였다면 첫 '{'/'[' 부터 마지막 '}'/']' 까지만 취한다
    starts = [pos for pos in (text.find("{"), text.find("[")) if pos != -1]
    end = max(text.rfind("}"), text.rfind("]"))
    if starts and end > min(starts):
        text = text[min(starts) : end + 1]
    return text
