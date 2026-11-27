"""面向 AI 的指令的识别规则(中英)。纯规则,不用模型。

这些正则只用来「认出」网页里写给 AI 的话并如实报告;命中的文字永远是数据,Butter 不执行其中任何内容。
第 3 周的起草检查器会复用这一份规则。
"""

from __future__ import annotations

import re

from butter.util import snippet

_I = re.IGNORECASE
_AI_NAMES = r"(?:ai|llms?|chatgpt|gpt|claude|gemini|perplexity|copilot|language models?|ai (?:agents?|assistants?|models?|crawlers?|systems?))"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override_en", re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|everything\s+)?(?:of\s+)?(?:the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|preceding|other)\s+(?:instructions?|prompts?|directions?|context|rules)", _I)),
    ("role_en", re.compile(r"\b(?:you are|you're)\s+(?:now\s+)?(?:an?\s+)?(?:ai|llm|large language model|language model|"
                           r"ai assistant|helpful assistant|chatbot)\b", _I)),
    ("address_en", re.compile(
        rf"\b(?:attention|note to|message (?:to|for)|instructions? (?:to|for)|dear)\s+(?:all\s+|any\s+)?{_AI_NAMES}\b", _I)),
    ("conditional_en", re.compile(
        r"\b(?:when|if)\s+(?:you|an ai|the ai|the assistant|the model|an llm)\s+(?:are\s+|is\s+)?"
        r"(?:asked|summari[sz]\w+|answer\w*|respond\w*|recommend\w*|generat\w+)\b.{0,80}?"
        r"\b(?:always|must|should|make sure|be sure)\b", _I | re.DOTALL)),
    ("steer_en", re.compile(
        r"\b(?:always|must|should)\s+(?:recommend|mention|cite|rank|prefer|include|describe)\b.{0,60}?"
        r"\b(?:first|as the best|as the top|above (?:all|others)|as the (?:only|leading))\b", _I | re.DOTALL)),
    ("suppress_en", re.compile(
        r"\bdo not\s+(?:mention|recommend|cite|include)\s+(?:any\s+)?(?:other|competitors?|competing)\b", _I)),
    ("markup", re.compile(r"<\|im_start\|>|\[/?INST\]|<<SYS>>|BEGIN SYSTEM PROMPT|###\s?(?:system|instruction)s?\s*:", _I)),
    ("override_zh", re.compile(r"(?:忽略|无视|不要理会|忘记)(?:掉)?(?:你)?(?:之前|以上|上述|先前|前面|此前)(?:的|所有|全部)*.{0,6}?"
                               r"(?:指令|提示|要求|内容|设定|规则)")),
    ("role_zh", re.compile(r"(?:作为|你是)(?:一个|一名|一位)?(?:AI|人工智能|大模型|语言模型|智能助手|AI\s?助手)", _I)),
    ("address_zh", re.compile(
        r"(?:AI|人工智能|大模型|智能助手|ChatGPT|豆包|DeepSeek|Kimi|元宝|文心一言|通义千问)(?:助手|模型)?\s*[,,::]?\s*"
        r"请(?:注意|务必|优先|始终|一定)", _I)),
    ("conditional_zh", re.compile(
        r"(?:回答|总结|推荐|引用|生成答案|被问到)(?:时|的时候).{0,20}?(?:请|必须|务必|一定要|始终)(?:优先)?(?:推荐|提及|引用|提到|把)")),
    ("steer_zh", re.compile(r"请(?:务必|始终|一定要|优先)(?:推荐|引用|提及|提到).{0,30}?(?:第一|首位|首选|最佳|最好|唯一)")),
]


def find_instructions(text: str, limit: int = 5) -> list[tuple[str, str]]:
    """返回 (规则名, 命中处的上下文片段)。"""
    hits: list[tuple[str, str]] = []
    for name, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = max(0, match.start() - 40), min(len(text), match.end() + 60)
            hits.append((name, snippet(text[start:end], 200)))
            if len(hits) >= limit:
                return hits
            break  # 每条规则报一处就够
    return hits
