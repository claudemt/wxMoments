from __future__ import annotations

from typing import Any


_COMMON_HANZI = frozenset(
    "的一是不了在人有我他這这個个們们中來来上大為为和國国地到以說说時时要就出會会可也你對对"
    "生能而子那得於于著着下自之年過过發发後后作裡里用道行所然家種种事成方多經经麼么去法學学"
    "如都同現现當当沒没動动面起看定天分還还進进好小部其些主樣样理心她本前開开但因只從从想實实"
    "日軍军者意無无力它與与長长把機机十民第公此已工使情明性知全三又關关點点正業业外將将兩两"
    "高間间由問问很最重並并物手應应戰战向頭头文體体政美相見见被利什二等產产或新己制身果加西"
    "斯月話话合回特代內内信表化老給给世位次度門门任常先海通教兒儿原東东聲声提立及比員员解水"
    "名真論论處处走義义各入幾几口認认條条平系氣气題题活爾尔更別别打女變变四神總总何電电數数"
    "安少報报才結结反受目太量再感建務务做接必場场件計计管期市直德資资命山金指克許许統统區区"
    "保至隊队形社便空決决治展馬马科司五基眼書书非則则聽听白卻却界達达光放強强即像難难且權权"
    "思王象完設设式色路記记南品住告類类求據据程北邊边死張张該该交規规萬万取拉格望覺觉術术領领"
    "共確确傳传師师觀观清今切院讓让識识候帶带導导爭争運运笑飛飞風风步改收根幹干造言聯联持組组"
    "每濟济車车親亲極极林服快辦办議议往元英士證证近失轉转夫令準准布始怎呢存未遠远叫臺台單单"
    "影具羅罗字愛爱擊击流備备兵連连調调深商算質质團团集百需價价花黨党華华城石級级整府離离況况"
    "亞亚請请技際际約约示復复病息究線线似官火斷断精滿满支視视消越器容照須须九增研寫写稱称企八"
    "功嗎吗包片史委乎查輕易早曾除農农找裝装廣广顯显吧阿李標标談谈吃圖图念六引歷历首醫医局突專专"
    "費费號号盡尽另周較较注語语僅仅考落青隨随選选列武紅红響响雖虽推勢势參参希古眾众構构房半節节"
    "土投某案黑維维革劃划敵敌致陳陈律足態态護护七興兴派孩驗验責责營营星夠够章音跟志底站嚴严巴例"
    "防族供效續续施留講讲型料終终答緊紧黃黄絕绝奇察母京段依批群項项故按河米圍围江織织害鬥斗雙双"
    "境客紀纪採采舉举殺杀攻父蘇苏密低朝友訴诉止細细願愿千值仍男錢钱破網网熱热助倒育屬属坐帝限"
    "船臉脸職职速刻樂乐否剛刚威毛狀状率甚獨独球般普怕彈弹校苦創创假久錯错承印晚蘭兰試试股拿腦脑"
    "預预誰谁益陽阳若哪微尼繼继送急血驚惊傷伤素藥药適适波夜省初喜衛卫源食險险待述陸陆習习置居"
    "勞劳財财環环排福納纳歡欢雷警獲获模充負负雲云停木遊游龍龙樹树疑層层冷洲衝冲射略範范竟句室"
    "異异激漢汉村哈策演簡简卡罪判擔担州靜静退既衣您宗積积餘余痛檢检差富靈灵協协角佔占配征修皮"
    "揮挥勝胜降階阶審审沉堅坚善媽妈劉刘讀读啊超免壓压銀银買买皇養养伊懷怀執执副亂乱抗犯追幫帮"
    "宣佛歲岁航優优怪香著田鐵铁控稅税左右份穿藝艺背陣阵草腳脚概惡恶塊块頓顿敢守酒島岛托央戶户"
    "烈洋哥索胡款靠評评版寶宝座釋释景顧顾弟登貨货互付伯慢歐欧換换聞闻危忙核暗姐介壞坏討讨麗丽"
)

_LEGACY_ENCODINGS = ("gb18030", "big5hkscs", "utf-16-le")


def _looks_broken(text: str) -> bool:
    sample = str(text or "")[:600]
    if not sample:
        return True
    if "\ufffd" in sample:
        return True
    printable = sum(1 for ch in sample if ch.isprintable() or ch in {"\n", "\r", "\t"})
    return (printable / len(sample)) < 0.85


def _looks_like_utf16(raw: bytes) -> bool:
    if raw[:2] in {b"\xff\xfe", b"\xfe\xff"}:
        return True
    head = raw[:64]
    if not head:
        return False
    return head.count(0) >= max(2, len(head) // 4)


def _guess_legacy_encoding(raw: bytes) -> str:
    try:
        from charset_normalizer import from_bytes
    except Exception:
        return ""
    try:
        match = from_bytes(raw).best()
    except Exception:
        return ""
    if match is None:
        return ""
    name = str(getattr(match, "encoding", "") or "").strip().lower()
    if name in {"gb18030", "gbk", "gb2312", "hz", "cp936", "ms936"}:
        return "gb18030"
    if name.startswith("big5") or name in {"cp950", "big5hkscs"}:
        return "big5hkscs"
    return ""


def _text_score(text: str) -> int:
    score = 0
    for ch in str(text or "")[:400]:
        code = ord(ch)
        if code < 0x20:
            if ch not in {"\n", "\r", "\t"}:
                score -= 8
        elif ch.isascii():
            score += 3
        elif 0x4E00 <= code <= 0x9FFF:
            score += 4
        elif 0x3400 <= code <= 0x4DBF:
            score += 3
        elif 0x20000 <= code <= 0x2FA1F:
            score += 1
        elif 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF or 0x2000 <= code <= 0x206F:
            score += 2
        elif 0xE000 <= code <= 0xF8FF:
            score -= 6
        elif 0x1F000 <= code <= 0x1FBFF:
            score += 2
        elif 0x0080 <= code <= 0x1EFF:
            score -= 6
        else:
            score += 1
    return score


def _hanzi_familiarity(text: str) -> int:
    return sum(1 for ch in str(text or "")[:400] if ch in _COMMON_HANZI)


def decode_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not isinstance(value, (bytes, bytearray)):
        return str(value)

    raw = bytes(value)
    if not raw:
        return ""

    utf8_text = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    else:
        if not _looks_broken(text):
            return text
        utf8_text = text

    for trim in (1, 2, 3):
        if len(raw) <= trim + 3:
            break
        try:
            text = raw[:-trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if (len(raw) - trim) >= len(raw) * 0.9 and not _looks_broken(text):
            return text

    if _looks_like_utf16(raw):
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                text = raw.decode(encoding).replace("\x00", "").strip()
            except UnicodeDecodeError:
                continue
            if text and not _looks_broken(text):
                return text

    encodings: list[str] = []
    guessed = _guess_legacy_encoding(raw)
    for encoding in (guessed, *_LEGACY_ENCODINGS):
        if encoding and encoding not in encodings:
            encodings.append(encoding)
    if len(raw) % 2 != 0 and "utf-16-le" in encodings:
        encodings.remove("utf-16-le")

    candidates: list[tuple[int, str]] = []
    for encoding in encodings:
        try:
            decoded = raw.decode(encoding).replace("\x00", "").strip()
        except UnicodeDecodeError:
            continue
        if not decoded or _looks_broken(decoded):
            continue
        rank = _text_score(decoded) + _hanzi_familiarity(decoded) * 3
        if encoding == guessed:
            rank += 2
        candidates.append((rank, decoded))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    if utf8_text:
        return utf8_text

    return raw.decode("utf-8", errors="ignore")


def is_readable(text: Any) -> bool:
    return not _looks_broken(text)
