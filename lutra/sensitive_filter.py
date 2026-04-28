"""Sensitive data filter — uses Mimo LLM to redact PII from text.

Redaction rules:
- Real names → role codes (e.g. 用户A, 开发者B)
- Phone numbers / emails → masked
- Internal URLs / IPs / tokens → removed
- Technical details preserved
"""

import logging

from openai import OpenAI

log = logging.getLogger("lutra.sensitive_filter")

_SYSTEM_PROMPT = """\
你是一个数据脱敏专家。请对以下文本进行脱敏处理，规则如下：

1. **人名** → 替换为角色代号（如"用户A"、"开发者B"），同一人名在全文中用同一代号
2. **手机号** → 替换为 1XX-XXXX-XXXX
3. **邮箱** → 替换为 xxx@example.com
4. **内部 URL**（含 .srv、.internal、内网域名）→ 替换为 [内部链接已移除]
5. **内网 IP 地址** (10.x / 172.16-31.x / 192.168.x) → 替换为 [IP已移除]
6. **Token / Secret / 密钥** → 替换为 [凭证已移除]
7. **保留所有技术细节**：错误信息、堆栈、代码片段、配置项名称、JIRA 编号等不做修改

直接输出脱敏后的文本，不要添加任何解释或前缀。"""

_CHUNK_SIZE = 6000  # chars per chunk


def filter_text(
    text: str,
    api_key: str,
    base_url: str,
    model: str,
    provider_id: str,
) -> str:
    """Filter sensitive data from text using Mimo.

    Falls back to returning original text if Mimo is unavailable.
    """
    if not text or not text.strip():
        return text

    if not api_key:
        log.warning("Mimo API key not configured, skipping filter")
        return text

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={"X-Model-Provider-Id": provider_id},
        )
    except Exception as e:
        log.warning("Failed to create Mimo client: %s", e)
        return text

    # Split into chunks by paragraphs
    chunks = _split_chunks(text, _CHUNK_SIZE)
    filtered_parts = []

    for i, chunk in enumerate(chunks):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": chunk},
                ],
                temperature=0,
            )
            filtered_parts.append(resp.choices[0].message.content)
        except Exception as e:
            log.warning("Mimo filter failed on chunk %d/%d: %s", i + 1, len(chunks), e)
            filtered_parts.append(chunk)

    return "\n".join(filtered_parts)


def _split_chunks(text: str, max_size: int) -> list[str]:
    """Split text into chunks by paragraphs, respecting max_size."""
    if len(text) <= max_size:
        return [text]

    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_size:
            chunks.append(current)
            current = para
        else:
            current = current + "\n\n" + para if current else para

    if current:
        chunks.append(current)

    # Handle single paragraphs longer than max_size
    result = []
    for chunk in chunks:
        if len(chunk) <= max_size:
            result.append(chunk)
        else:
            for i in range(0, len(chunk), max_size):
                result.append(chunk[i:i + max_size])

    return result
