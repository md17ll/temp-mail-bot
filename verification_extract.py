import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import List, Tuple
from urllib.parse import urlsplit


VERIFY_WORDS = re.compile(
    r"(?i)(?:\botp\b|\bpin\b|one[ -]?time|passcode|verification|verify|authentication|"
    r"security code|login code|confirmation code|رمز(?:\s+(?:التحقق|التأكيد|الأمان|الدخول))?|"
    r"كود\s+(?:التحقق|التأكيد|الأمان|الدخول)|كلمة مرور لمرة واحدة|الرقم السري|تحقق|تأكيد|"
    r"doğrulama|verificaci[oó]n|vérification|bestätigung|인증|验证码|認証)"
)
LINK_WORDS = re.compile(
    r"(?i)(?:activate|activation|verify|verification|confirm(?:ation)?|reset(?: password)?|"
    r"recover(?:y)?|magic[ -]?link|complete (?:signup|registration)|sign[ -]?in|login|"
    r"تفعيل|فعّل|تحقق|تأكيد|أكد|إعادة تعيين|استعادة|تغيير كلمة المرور|تسجيل الدخول)"
)
PROMO_WORDS = re.compile(
    r"(?i)(?:coupon|promo(?:tion)?|discount|offer|sale|voucher|deal|خصم|عرض|عروض|قسيمة|ترويجي)"
)
NON_CODE_WORDS = re.compile(
    r"(?i)(?:order|invoice|receipt|amount|price|total|tracking|customer|reference|"
    r"رقم الطلب|الفاتورة|فاتورة|السعر|المبلغ|الإجمالي|التتبع|رقم العميل|المرجع)"
)
BAD_LINK_WORDS = re.compile(
    r"(?i)(?:unsubscribe|preferences|privacy|terms|view.{0,8}(?:browser|web)|facebook|instagram|"
    r"twitter|tiktok|linkedin|youtube|شراء|تسوق|unsubscribe|إلغاء الاشتراك|سياسة الخصوصية|الشروط)"
)
URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.I)


@dataclass
class SmartFindings:
    codes: List[str]
    links: List[str]


def _arabic_digits(value: str) -> str:
    return value.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))


def _clean_url(value: str) -> str:
    return unescape(value or "").strip().rstrip(".,;:!؟)]}>")


def _valid_web_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username
    except Exception:
        return False


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[Tuple[str, List[str]]] = []
        self.links: List[Tuple[str, str]] = []

    def handle_starttag(self, tag, attrs) -> None:
        if str(tag).lower() != "a":
            return
        href = dict(attrs).get("href") or ""
        self.stack.append((href, []))

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1][1].append(data)

    def handle_endtag(self, tag) -> None:
        if str(tag).lower() == "a" and self.stack:
            href, parts = self.stack.pop()
            self.links.append((href, " ".join(parts).strip()))


def _unique(items: List[str], limit: int) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
        if len(result) >= limit:
            break
    return result


def extract_verification_codes(subject: str, body: str) -> List[str]:
    text = _arabic_digits(f"{subject or ''}\n{body or ''}")
    # URLs contain IDs and signatures that must never be mistaken for OTP codes.
    scan = URL_RE.sub(" ", text)
    candidates: List[Tuple[int, int, str]] = []

    numeric = re.compile(r"(?<![\w])((?:\d[ \-]{0,2}){3,7}\d)(?![\w])")
    alnum = re.compile(r"(?<![A-Za-z0-9])([A-Z0-9]{4,10})(?![A-Za-z0-9])")
    for match in numeric.finditer(scan):
        code = re.sub(r"[ \-]", "", match.group(1))
        if not 4 <= len(code) <= 8:
            continue
        window = scan[max(0, match.start() - 170): min(len(scan), match.end() + 170)]
        if not VERIFY_WORDS.search(window):
            continue
        near_candidate = scan[max(0, match.start() - 35): min(len(scan), match.end() + 35)]
        if NON_CODE_WORDS.search(near_candidate):
            continue
        score = 100
        if len(code) in (4, 6):
            score += 20
        if len(code) == 4 and code.startswith(("19", "20")):
            score -= 50
        if PROMO_WORDS.search(window) and not re.search(r"(?i)(otp|security|رمز التحقق|كود التحقق|تسجيل الدخول)", window):
            score -= 90
        candidates.append((score, match.start(), code))

    for match in alnum.finditer(scan):
        code = match.group(1)
        if code.isdigit() or code.isalpha() or not (re.search(r"[A-Z]", code) and re.search(r"\d", code)):
            continue
        window = scan[max(0, match.start() - 140): min(len(scan), match.end() + 140)]
        if VERIFY_WORDS.search(window) and not PROMO_WORDS.search(window):
            candidates.append((115, match.start(), code))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return _unique([item[2] for item in candidates if item[0] >= 100], 5)


def extract_action_links(subject: str, body: str, html_body: str = "") -> List[str]:
    candidates: List[Tuple[str, str]] = []
    if html_body:
        parser = _LinkParser()
        try:
            parser.feed(html_body)
            parser.close()
            candidates.extend(parser.links)
        except Exception:
            pass

    plain = f"{subject or ''}\n{body or ''}"
    for match in URL_RE.finditer(plain):
        context = plain[max(0, match.start() - 110): min(len(plain), match.end() + 110)]
        candidates.append((match.group(0), context))

    accepted: List[str] = []
    for raw_url, label in candidates:
        url = _clean_url(raw_url)
        if not _valid_web_url(url):
            continue
        evidence = f"{label} {url}"
        if BAD_LINK_WORDS.search(evidence):
            continue
        if PROMO_WORDS.search(label) and not LINK_WORDS.search(label):
            continue
        if LINK_WORDS.search(evidence):
            accepted.append(url)
    return _unique(accepted, 5)


def extract_smart_findings(subject: str, body: str, html_body: str = "") -> SmartFindings:
    return SmartFindings(
        codes=extract_verification_codes(subject, body),
        links=extract_action_links(subject, body, html_body),
    )
