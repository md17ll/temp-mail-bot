import re
from html import unescape
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup


BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "div", "dl", "dt", "dd",
    "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
SKIP_TAGS = {"script", "style", "head", "noscript", "svg", "template"}


class ReadableHTMLParser(HTMLParser):
    """Convert HTML email content to readable text without CSS/script/schema noise."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip_depth = 0
        self.link_stack: List[Tuple[Optional[str], int]] = []

    def _newline(self) -> None:
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}

        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        if tag in BLOCK_TAGS:
            self._newline()

        if tag == "a":
            self.link_stack.append((attrs_dict.get("href") or None, len(self.parts)))
        elif tag == "img":
            alt = (attrs_dict.get("alt") or "").strip()
            if alt and len(alt) <= 120:
                self.parts.append(alt)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return

        if tag == "a" and self.link_stack:
            href, start_index = self.link_stack.pop()
            anchor_text = "".join(self.parts[start_index:]).strip()
            if href and anchor_text and href.startswith(("http://", "https://")):
                normalized_href = unescape(href).strip()
                if normalized_href and normalized_href not in anchor_text:
                    self.parts.append(f" ({normalized_href})")

        if tag in BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if data:
            self.parts.append(data)

    def get_text(self) -> str:
        return clean_text("".join(self.parts))


def clean_text(value: str) -> str:
    text = unescape(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[\u200c-\u200f\u202a-\u202e\u2060\ufeff]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    lines: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lower = line.lower()
        if lower.startswith("@import ") or lower.startswith("@font-face"):
            continue
        if "fonts.googleapis.com/css" in lower and len(line) < 500:
            continue
        lines.append(line)

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip()


def html_to_readable_text(html_value: str) -> str:
    if not html_value:
        return ""
    parser = ReadableHTMLParser()
    try:
        parser.feed(html_value)
        parser.close()
        return parser.get_text()
    except Exception:
        text = re.sub(r"(?is)<(script|style|head|svg|template).*?>.*?</\1>", " ", html_value)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])\s*>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return clean_text(text)


OTP_KEYWORDS = re.compile(
    r"(?i)(?:"
    r"\botp\b|\bpin\b|\bpasscode\b|\bone[- ]time(?: password| code)?\b|"
    r"verification(?: code)?|verify(?:ing| your)?(?: account)?|security code|confirmation code|"
    r"authentication code|login code|access code|验证码|認証コード|인증 ?코드|"
    r"رمز(?: التحقق| التأكيد| الأمان| الدخول)?|كود(?: التحقق| التأكيد| الأمان)?|"
    r"الرقم السري|رمز لمرة واحدة|كلمة مرور لمرة واحدة|تحقق|تأكيد|"
    r"doğrulama kodu|codigo de verificacion|código de verificación|"
    r"code de vérification|bestätigungscode|codice di verifica"
    r")"
)

NUMERIC_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
ALNUM_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{4,10})(?![A-Za-z0-9])")


def _line_for(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].strip()


def _has_otp_context(text: str, start: int, end: int, radius: int = 180) -> bool:
    window = text[max(0, start - radius): min(len(text), end + radius)]
    return bool(OTP_KEYWORDS.search(window))


def _candidate_score(text: str, start: int, end: int, candidate: str) -> int:
    window_start = max(0, start - 180)
    window_end = min(len(text), end + 180)
    window = text[window_start:window_end]
    score = 0

    keyword_matches = list(OTP_KEYWORDS.finditer(window))
    if keyword_matches:
        score += 100
        candidate_center = start - window_start + (end - start) // 2
        distance = min(
            abs(candidate_center - ((m.start() + m.end()) // 2)) for m in keyword_matches
        )
        score += max(0, 50 - distance // 4)

    if _line_for(text, start, end) == candidate:
        score += 30

    if candidate.isdigit():
        if len(candidate) in (4, 6):
            score += 20
        elif len(candidate) in (5, 7, 8):
            score += 10
        if len(candidate) == 4 and candidate.startswith(("19", "20")):
            score -= 35
    elif candidate.isalpha():
        # Letter-only OTPs are accepted only when they are uppercase, short,
        # displayed on their own line, and near an explicit OTP/PIN keyword.
        if (
            candidate.isupper()
            and 4 <= len(candidate) <= 8
            and _line_for(text, start, end) == candidate
            and _has_otp_context(text, start, end)
        ):
            score += 25
        else:
            score -= 100
    else:
        if not (re.search(r"[A-Za-z]", candidate) and re.search(r"\d", candidate)):
            score -= 100
        else:
            score += 25 if len(candidate) == 4 else 15

    return score


def extract_otp(subject: str, body: str) -> Optional[str]:
    """Return an OTP/PIN only when contextual evidence indicates it is a real code."""
    combined = clean_text(f"{subject or ''}\n{body or ''}")
    candidates: List[Tuple[int, int, str, int]] = []

    for match in NUMERIC_CODE_RE.finditer(combined):
        code = match.group(1)
        score = _candidate_score(combined, match.start(1), match.end(1), code)
        candidates.append((score, match.start(1), code, len(code)))

    for match in ALNUM_CODE_RE.finditer(combined):
        code = match.group(1)
        if code.isdigit():
            continue

        if code.isalpha():
            if not (
                code.isupper()
                and 4 <= len(code) <= 8
                and _line_for(combined, match.start(1), match.end(1)) == code
                and _has_otp_context(combined, match.start(1), match.end(1))
            ):
                continue
        elif not (re.search(r"[A-Za-z]", code) and re.search(r"\d", code)):
            continue

        score = _candidate_score(combined, match.start(1), match.end(1), code)
        candidates.append((score, match.start(1), code, len(code)))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    best_score, _, best_code, _ = candidates[0]

    if best_score < 100:
        return None
    return best_code


def otp_copy_keyboard(code: Optional[str]) -> Optional[InlineKeyboardMarkup]:
    if not code:
        return None
    try:
        button = InlineKeyboardButton(
            text=f"📋 {code}",
            copy_text=CopyTextButton(text=code),
        )
    except TypeError:
        button = InlineKeyboardButton(
            text=f"📋 {code}",
            api_kwargs={"copy_text": {"text": code}},
        )
    return InlineKeyboardMarkup([[button]])


def split_message(text: str, limit: int = 3900) -> List[str]:
    """Split long Telegram messages without dropping any email content."""
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < int(limit * 0.55):
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut < int(limit * 0.55):
            cut = limit
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
