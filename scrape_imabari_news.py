#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape_imabari_news.py

今治市公式サイトの更新情報ページ（whatsnew.html）を巡回し、
過去7日分のお知らせを抽出。各お知らせの詳細ページも取得して、
タイトルと要約をどちらも読みやすい日本語に言い換えて
docs/news.json として書き出す。

言い換えは2段構え：
  1. Google AI Studio（Gemini API）の無料枠が使えれば、そちらでAIによる
     やさしい言い換えを作る。クレジットカード登録不要・期限なしの無料枠。
     （2026年8月時点で確認。Googleアカウントでのサインアップと、
       GitHub Actions側でのAPIキー登録（Secrets）は必要）
  2. GEMINI_API_KEY が未設定、またはAPI呼び出しに失敗した場合
     （レート制限・一時的な障害など）は、ルールベースの言い換えに
     自動的にフォールバックする（こちらは完全無料・登録不要）

一度AIでの言い換えに成功した項目は、前回の docs/news.json に
"ai_rewritten": true として保存され、以降の実行では再取得・再要約を
行わずそのまま使い回す（load_previous_ai_results()）。ルールベースの
ままだった項目・新規に追加された項目だけを毎回あらためて処理する。
掲載から7日を過ぎた項目は、これまで通り一覧から自動的に外れる。

GitHub Actions などから1日に複数回実行することを想定している。

依存: requests, beautifulsoup4
    pip install requests beautifulsoup4
"""

import json
import os
import re
import time
import datetime
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.city.imabari.ehime.jp/whatsnew.html"
SITE_ROOT = "https://www.city.imabari.ehime.jp/"
OUTPUT_PATH = Path(__file__).parent / "docs" / "news.json"

# 今治市サイトへの負荷を抑えるための最低限のマナー設定
REQUEST_DELAY_SEC = 1.5
REQUEST_TIMEOUT = 15
HEADERS = {
    "User-Agent": "ImabariNewsBoardBot/1.0 (+personal project; contact: set-your-contact-here)"
}

DAYS_TO_KEEP = 7
MAX_SUMMARY_CHARS = 160

# --- Google AI Studio / Gemini API（無料枠。クレジットカード不要・期限なし） ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
# モデル名はGoogle側の変更で通らなくなることがあるため、上から順に試す。
GEMINI_MODEL_CANDIDATES = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash"]
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# 実行中に一度成功したモデル名はここにキャッシュして、以降はそれだけを使う
_GEMINI_WORKING_MODEL = None

# 無料枠の「1分あたりのリクエスト数(RPM)」制限に当たらないよう、
# Gemini呼び出しの間隔をこれ以上空ける（安全側に余裕を持たせた値）。
GEMINI_MIN_INTERVAL_SEC = 4.5
_gemini_last_call_at = 0.0
# 429（レート制限）が出た場合、これだけ待って1回だけ再試行する
GEMINI_RETRY_WAIT_SEC = 20

CATEGORY_RULES = [
    ("交通・航路", ["航路", "運航", "交通規制", "運休", "フェリー", "渡船"]),
    ("募集・イベント", ["募集", "開催", "講座", "研修", "説明会", "ワークショップ", "コンテスト", "フェスティバル"]),
    ("補助金・支援", ["補助", "助成", "応援金", "支援金", "給付"]),
    ("入札・プロポーザル", ["プロポーザル", "入札", "公募"]),
]

# タイトルの「お役所文型」→自然な言い方への変換ルール（Geminiが使えない
# 場合のフォールバック用）。上から順に試し、最初にマッチしたものを適用。
TITLE_RULES = [
    (re.compile(r"「(.+?)」に係る公募型プロポーザルの実施について、?(質問及び回答|選定結果)を?掲載しました。?$"),
     lambda m: f"「{m.group(1)}」の{m.group(2)}を公開しました"),
    (re.compile(r"「(.+?)」に係る公募型プロポーザルの実施について。?$"),
     lambda m: f"「{m.group(1)}」の委託・受託事業者を募集します"),
    (re.compile(r"「(.+?)」の選定結果について。?$"),
     lambda m: f"「{m.group(1)}」の選定結果を発表しました"),
    (re.compile(r"(.+?)の選定結果について。?$"), lambda m: f"{m.group(1)}の選定結果を発表しました"),
    (re.compile(r"(.+?)の開催について。?$"), lambda m: f"{m.group(1)}を開催します"),
    (re.compile(r"(.+?)の募集について。?$"), lambda m: f"{m.group(1)}の参加者を募集します"),
    (re.compile(r"(.+?)の実施について。?$"), lambda m: f"{m.group(1)}を行います"),
    (re.compile(r"(.+?)を掲載しました。?$"), lambda m: f"{m.group(1)}を公開しました"),
    (re.compile(r"(.+?)について、?質問及び回答を掲載しました。?$"), lambda m: f"{m.group(1)}の質問・回答を公開しました"),
    (re.compile(r"(.+?)について。?$"), lambda m: f"{m.group(1)}のお知らせ"),
]

# 要約を組み立てる際に拾いたい見出しラベルと、そこに対応するキーワード
# （フォールバック用）
SUMMARY_FIELD_RULES = [
    ("対象", ["対象", "参加"]),
    ("期間・日程", ["期間", "日程", "日時", "受付期間", "実施日"]),
    ("金額", ["金額", "補助金額", "料金", "費用"]),
    ("申込方法", ["申込", "申請方法", "応募方法", "参加方法", "登録"]),
    ("問い合わせ", ["問い合わせ", "お問い合わせ"]),
]


def guess_category(title: str) -> str:
    for cat, keywords in CATEGORY_RULES:
        if any(k in title for k in keywords):
            return cat
    return "お知らせ"


def simplify_title_rule_based(title: str) -> str:
    """行政特有の言い回しを、自然な言い方に置き換える（ルールベース・無料）。"""
    t = title.strip()
    for pattern, repl in TITLE_RULES:
        m = pattern.match(t)
        if m:
            return repl(m)
    return t


def fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return BeautifulSoup(resp.text, "html.parser")


def parse_whatsnew(soup: BeautifulSoup):
    """
    更新情報ページから (date, title, url) のリストを抽出する。
    サイト構造が変わった場合はここを直す。
    """
    items = []
    main = soup.find(id="main_container") or soup
    date_pattern = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")

    for node in main.find_all(string=date_pattern):
        m = date_pattern.search(node)
        if not m:
            continue
        y, mo, d = map(int, m.groups())
        try:
            date = datetime.date(y, mo, d)
        except ValueError:
            continue

        link_el = node.parent.find_next("a")
        if link_el is None:
            continue

        title = link_el.get_text(strip=True)
        href = link_el.get("href", "")
        if not title or not href:
            continue
        url = urljoin(SITE_ROOT, href)
        items.append({"date": date.isoformat(), "title": title, "url": url})

    return items


def extract_lead_text(main) -> str:
    """タイトル(h1)直後の説明文だけを狙って抜き出す。"""
    h1 = main.find("h1")
    texts = []
    if h1:
        for el in h1.find_all_next():
            if el.name in ("h2", "h3"):
                break
            if el.name in ("p", "li"):
                t = el.get_text(strip=True)
                if len(t) > 15:
                    texts.append(t)
            if len(texts) >= 3:
                break
    if not texts:
        texts = [
            p.get_text(strip=True)
            for p in main.find_all(["p", "li"])
            if len(p.get_text(strip=True)) > 15
        ][:3]
    return re.sub(r"\s+", " ", " ".join(texts)).strip()


def extract_structured_summary(main) -> str | None:
    """
    ページ自身が使っている見出し（対象／期間／金額 など）を拾って、
    「対象: 〜／期間: 〜」の形に組み立てる（フォールバック用）。
    """
    found = {}
    for h in main.find_all(["h2", "h3"]):
        htext = h.get_text(strip=True)
        for label, keywords in SUMMARY_FIELD_RULES:
            if label in found:
                continue
            if any(k in htext for k in keywords):
                nxt = h.find_next(["p", "li", "table"])
                if not nxt:
                    continue
                t = nxt.get_text(" ", strip=True)
                t = re.sub(r"\s+", " ", t).strip()
                if not t:
                    continue
                if len(t) > 45:
                    t = t[:45] + "…"
                found[label] = t
                break
    if not found:
        return None
    if list(found.keys()) == ["問い合わせ"]:
        return None
    return "　".join(f"{k}：{v}" for k, v in found.items())


def _wait_for_gemini_rate_limit():
    """前回のGemini呼び出しから GEMINI_MIN_INTERVAL_SEC 秒は空ける。"""
    global _gemini_last_call_at
    elapsed = time.monotonic() - _gemini_last_call_at
    remaining = GEMINI_MIN_INTERVAL_SEC - elapsed
    if remaining > 0:
        time.sleep(remaining)


def _call_gemini_once(model: str, prompt: str):
    """Gemini APIを1回呼び出す。成功時は (title, summary)、404時は 'not_found'、
    429（レート制限）や503等の一時的な障害時は 'retryable'、その他失敗時は None を返す。"""
    global _gemini_last_call_at

    _wait_for_gemini_rate_limit()
    url = GEMINI_URL_TMPL.format(model=model)
    try:
        resp = requests.post(
            url,
            params={"key": GEMINI_API_KEY},
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.3,
                    "responseMimeType": "application/json",
                },
            },
            timeout=30,
        )
        _gemini_last_call_at = time.monotonic()

        if resp.status_code == 404:
            print(f"[WARN] モデル '{model}' が404。詳細: {resp.text[:300]}", file=sys.stderr)
            return "not_found"
        if resp.status_code == 429:
            print(f"[WARN] モデル '{model}' が429（レート制限）。詳細: {resp.text[:300]}", file=sys.stderr)
            return "retryable"
        if resp.status_code in (503, 500, 502, 504):
            print(f"[WARN] モデル '{model}' が{resp.status_code}（一時的な混雑・障害）。詳細: {resp.text[:300]}", file=sys.stderr)
            return "retryable"

        resp.raise_for_status()
        data = resp.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        parsed = json.loads(content)
        plain_title = parsed.get("title", "").strip()
        plain_summary = parsed.get("summary", "").strip()
        if plain_title and plain_summary:
            return (plain_title, plain_summary)
        return None
    except requests.exceptions.HTTPError as e:
        _gemini_last_call_at = time.monotonic()
        body = e.response.text[:300] if e.response is not None else ""
        print(f"[WARN] Gemini呼び出しに失敗（モデル: {model}）: {e}\n  詳細: {body}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        _gemini_last_call_at = time.monotonic()
        print(f"[WARN] Gemini呼び出しに失敗（モデル: {model}）: {e}", file=sys.stderr)
        return None


def rewrite_with_gemini(title: str, lead_text: str):
    """
    Google AI Studio（Gemini API・無料枠）で、タイトルと要約をまとめて
    やさしい日本語に言い換える。GEMINI_API_KEY未設定や失敗時は None を
    返す（呼び出し側でルールベースにフォールバックする）。

    - モデル名は GEMINI_MODEL_CANDIDATES を順番に試し、最初に成功した
      モデル名を以降の呼び出しでも使い回す（毎回全部試すと遅くなるため）。
    - 呼び出し間隔は GEMINI_MIN_INTERVAL_SEC 以上空け、429（レート制限）や
      503（一時的な混雑）が出た場合は GEMINI_RETRY_WAIT_SEC 秒待って
      1回だけ再試行する。
    """
    global _GEMINI_WORKING_MODEL

    if not GEMINI_API_KEY or not lead_text:
        return None

    prompt = (
        "あなたは自治体広報の編集者です。以下は今治市公式サイトのお知らせの"
        "タイトルと本文抜粋です。専門用語や「〜について」「〜に係る」のような"
        "硬い言い回しを避け、一般市民が一読して内容と自分に関係あるかが"
        "分かるように書き直してください。日付・金額・締切など具体的な数字は"
        "省略せず残してください。\n\n"
        f"【元のタイトル】{title}\n【本文抜粋】{lead_text}\n\n"
        "次のJSON形式のみで出力してください（前置き・コードブロック記号は不要）：\n"
        '{"title": "やさしい言い換えタイトル（30字程度）", '
        '"summary": "やさしい要約（2文以内・120字程度）"}'
    )

    candidates = [_GEMINI_WORKING_MODEL] if _GEMINI_WORKING_MODEL else GEMINI_MODEL_CANDIDATES

    for model in candidates:
        result = _call_gemini_once(model, prompt)

        if result == "not_found":
            continue  # 次のモデル候補へ

        if result == "rate_limited" or result == "retryable":
            print(f"[INFO] {GEMINI_RETRY_WAIT_SEC}秒待って1回だけ再試行します。", file=sys.stderr)
            time.sleep(GEMINI_RETRY_WAIT_SEC)
            result = _call_gemini_once(model, prompt)
            if result in ("not_found", "rate_limited", "retryable", None):
                print("[WARN] 再試行も失敗。この件はルールベースにフォールバックします。", file=sys.stderr)
                return None

        if isinstance(result, tuple):
            _GEMINI_WORKING_MODEL = model
            return result

        return None

    print("[WARN] 候補モデルすべてで404。ルールベースを使用します。", file=sys.stderr)
    return None


def rewrite_detail_page(url: str, official_title: str):
    """
    詳細ページを取得し、(表示用タイトル, 要約, AIで言い換えたか) を返す。
    PDFや外部サイトは本文取得をスキップする。
    """
    if url.lower().endswith(".pdf"):
        return simplify_title_rule_based(official_title), "（PDF資料）詳細は今治市サイトのPDFをご確認ください。", False
    if "city.imabari.ehime.jp" not in url:
        return simplify_title_rule_based(official_title), "（外部サイトの情報です）詳細はリンク先をご確認ください。", False

    try:
        soup = fetch(url)
    except Exception as e:  # noqa: BLE001
        return simplify_title_rule_based(official_title), f"（本文の取得に失敗しました: {e}）", False

    main = soup.find(id="main_container") or soup
    lead_text = extract_lead_text(main)

    # 1. Geminiが使えれば、タイトル・要約をまとめてAIで言い換え
    ai_result = rewrite_with_gemini(official_title, lead_text)
    if ai_result:
        return ai_result[0], ai_result[1], True

    # 2. フォールバック：ルールベースの言い換え
    plain_title = simplify_title_rule_based(official_title)
    structured = extract_structured_summary(main)
    if structured:
        summary = structured
    elif lead_text:
        summary = lead_text
    else:
        summary = "本文の要約を作成できませんでした。詳細はリンク先をご確認ください。"
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + "…"
    return plain_title, summary, False


def load_previous_ai_results():
    """
    前回書き出した docs/news.json のうち、AIでの言い換えに成功していた
    項目だけを url をキーにした辞書で返す。ファイルが無い・壊れている
    場合は空の辞書を返す（初回実行などは通常のフローになる）。
    """
    if not OUTPUT_PATH.exists():
        return {}
    try:
        data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 前回の news.json の読み込みに失敗（無視して続行）: {e}", file=sys.stderr)
        return {}

    cache = {}
    for it in data.get("items", []):
        if it.get("ai_rewritten") and it.get("url"):
            cache[it["url"]] = it
    print(f"[INFO] 前回AI言い換え済みの項目: {len(cache)}件をキャッシュとして利用します。", file=sys.stderr)
    return cache


def main():
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=DAYS_TO_KEEP - 1)

    if GEMINI_API_KEY:
        print("[INFO] GEMINI_API_KEY を検出。Geminiでの言い換えを試みます。", file=sys.stderr)
    else:
        print("[INFO] GEMINI_API_KEY 未設定。ルールベースの言い換えのみ使用します。", file=sys.stderr)

    ai_cache = load_previous_ai_results()

    print(f"[INFO] fetching {BASE_URL}", file=sys.stderr)
    soup = fetch(BASE_URL)
    all_items = parse_whatsnew(soup)
    print(f"[INFO] parsed {len(all_items)} items total", file=sys.stderr)

    # 掲載から7日を過ぎたものはここで除外される（=これまで通り自動的に消える）
    recent = [it for it in all_items if datetime.date.fromisoformat(it["date"]) >= cutoff]
    print(f"[INFO] {len(recent)} items within last {DAYS_TO_KEEP} days", file=sys.stderr)

    results = []
    reused, freshly_processed = 0, 0
    for it in recent:
        cached = ai_cache.get(it["url"])
        if cached:
            # 既にAIで言い換え済み：再取得・再要約はせず、前回の内容をそのまま使う
            print(f"[INFO] キャッシュ利用（AI言い換え済み）: {it['title'][:40]}", file=sys.stderr)
            results.append({
                "date": it["date"],
                "category": cached.get("category", guess_category(it["title"])),
                "title": cached["title"],
                "official_title": it["title"],
                "summary": cached["summary"],
                "url": it["url"],
                "ai_rewritten": True,
            })
            reused += 1
            continue

        print(f"[INFO] summarizing: {it['title'][:40]}", file=sys.stderr)
        plain_title, summary, ai_rewritten = rewrite_detail_page(it["url"], it["title"])
        results.append({
            "date": it["date"],
            "category": guess_category(it["title"]),
            "title": plain_title,
            "official_title": it["title"],
            "summary": summary,
            "url": it["url"],
            "ai_rewritten": ai_rewritten,
        })
        freshly_processed += 1
        time.sleep(REQUEST_DELAY_SEC)

    print(f"[INFO] キャッシュ再利用: {reused}件 / 新規処理: {freshly_processed}件", file=sys.stderr)

    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": BASE_URL,
        "items": results,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[INFO] wrote {len(results)} items to {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
