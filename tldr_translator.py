#!/usr/bin/env python3
"""
TLDR Newsletter Translator
Gmail(IMAP)에서 TLDR 뉴스레터를 가져와서 DeepL로 번역 후 Slack으로 발송
GitHub Actions에서 실행 가능
"""

import imaplib
import email
from email.header import decode_header
import json
import os
import re
import requests
from datetime import datetime, timedelta
from pathlib import Path

# 환경 변수에서 설정 읽기 (GitHub Actions 호환)
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DEEPL_API_URL = "https://api-free.deepl.com/v2/translate"

# 처리된 ID 파일 경로
PROCESSED_IDS_PATH = Path(__file__).parent / "processed_ids.json"

# TLDR 종류별 이모지
TLDR_TYPE_EMOJI = {
    "TLDR": "📬",
    "TLDR AI": "🤖",
    "TLDR InfoSec": "🔐",
    "TLDR Crypto": "₿",
    "TLDR Founders": "🚀",
    "TLDR Design": "🎨",
    "TLDR Marketing": "📈",
    "TLDR DevOps": "⚙️",
    "TLDR Web": "🌐",
}


def get_processed_ids():
    if PROCESSED_IDS_PATH.exists():
        with open(PROCESSED_IDS_PATH) as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids):
    with open(PROCESSED_IDS_PATH, "w") as f:
        json.dump(list(ids), f)


def connect_gmail():
    """Gmail IMAP 연결"""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    return mail


def search_tldr_emails(mail, days_back=1):
    """TLDR 뉴스레터 검색"""
    mail.select("inbox")

    # 날짜 기준 검색
    since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
    search_query = f'(FROM "tldrnewsletter.com" SINCE {since_date})'

    status, messages = mail.search(None, search_query)

    if status != "OK":
        return []

    return messages[0].split()


def decode_mime_header(header):
    """MIME 헤더 디코딩"""
    if not header:
        return ""

    decoded_parts = decode_header(header)
    result = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)


def get_email_content(mail, msg_id):
    """이메일 내용 가져오기"""
    status, msg_data = mail.fetch(msg_id, "(RFC822)")

    if status != "OK":
        return None

    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)

    # 헤더 추출
    subject = decode_mime_header(msg.get("Subject", ""))
    from_header = decode_mime_header(msg.get("From", ""))
    message_id = msg.get("Message-ID", msg_id.decode())

    # TLDR 종류 추출
    tldr_type = "TLDR"
    if match := re.search(r"TLDR\s*(\w+)", from_header):
        tldr_type = f"TLDR {match.group(1)}"

    # 본문 추출
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="ignore")

    return {
        "id": message_id,
        "subject": subject,
        "tldr_type": tldr_type,
        "body": body
    }


def extract_links(body):
    """이메일 끝의 Links 섹션에서 URL 추출"""
    links = {}
    links_match = re.search(r"Links:\s*-+\s*(.*)", body, re.DOTALL)
    if links_match:
        links_section = links_match.group(1)
        for match in re.finditer(r"\[(\d+)\]\s*(https?://[^\s]+)", links_section):
            links[match.group(1)] = match.group(2)
    return links


def parse_articles(body, links):
    """기사 파싱 - 제목, 링크, 요약 추출"""
    articles = []
    current_section = ""

    lines = body.split("\n")
    i = 0

    section_emoji = {
        "HEADLINES": "📰", "LAUNCHES": "🚀",
        "DEEP DIVES": "🧠", "ANALYSIS": "🔍",
        "ENGINEERING": "🧑‍💻", "RESEARCH": "🔬",
        "MISCELLANEOUS": "🎁", "QUICK LINKS": "⚡",
        "BIG TECH": "🏢", "STARTUPS": "🌱",
        "SCIENCE": "🔭", "PROGRAMMING": "💻",
        "SECURITY": "🔒", "OPINIONS": "💭",
    }

    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # 섹션 헤더 감지
        if re.match(r"^[🚀🧠🧑‍💻🎁⚡📰🔒🎨💰📣👔🔭💻🏢🌱💭]\s*$", line):
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line and re.match(r"^[A-Z][A-Z\s&]+$", next_line):
                    current_section = next_line.title()
                    for key, emoji in section_emoji.items():
                        if key in next_line.upper():
                            current_section = f"{emoji} {next_line.title()}"
                            break
                    i += 2
                    continue
            i += 1
            continue

        # 기사 제목 패턴
        title_match = re.match(
            r"^(.+?)\s*\((\d+)\s*MINUTE\s*READ\)\s*\[(\d+)\]",
            line,
            re.IGNORECASE
        )

        if title_match:
            title = title_match.group(1).strip()
            link_num = title_match.group(3)
            url = links.get(link_num, "")

            description_lines = []
            i += 1
            empty_line_count = 0

            while i < len(lines):
                desc_line = lines[i].strip()

                if not desc_line:
                    empty_line_count += 1
                    if empty_line_count >= 2:
                        break
                    i += 1
                    continue

                empty_line_count = 0

                if re.match(r"^.+\(\d+\s*MINUTE\s*READ\)\s*\[\d+\]", desc_line, re.IGNORECASE):
                    break
                if re.match(r"^[🚀🧠🧑‍💻🎁⚡📰🔒🎨💰📣👔🔭💻🏢🌱💭]\s*$", desc_line):
                    break
                if any(skip in desc_line.lower() for skip in [
                    "sign up", "advertise", "unsubscribe", "manage your",
                    "referral", "tldr is hiring", "want to work", "love tldr"
                ]):
                    break

                description_lines.append(desc_line)
                i += 1

            description = " ".join(description_lines)
            summary = get_summary(description)

            if title and url:
                articles.append({
                    "section": current_section,
                    "title": title,
                    "url": url,
                    "summary": summary
                })
            continue

        i += 1

    return articles


def get_summary(text):
    """첫 2문장 추출"""
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sentences[:2])
    if len(summary) > 300:
        summary = summary[:297] + "..."
    return summary


def translate_text(text, target_lang="KO"):
    """DeepL API로 텍스트 번역"""
    if not text:
        return ""

    response = requests.post(
        DEEPL_API_URL,
        data={
            "auth_key": DEEPL_API_KEY,
            "text": text,
            "target_lang": target_lang
        }
    )

    if response.status_code == 200:
        result = response.json()
        return result["translations"][0]["text"]
    else:
        print(f"DeepL API 오류: {response.status_code}")
        return text


def translate_articles(articles):
    """기사 번역 (제목 + 요약) - 개별 번역"""
    if not articles:
        return []

    result = []
    for article in articles:
        translated_title = translate_text(article["title"])
        translated_summary = translate_text(article["summary"])

        result.append({
            "section": article["section"],
            "title": translated_title.strip(),
            "url": article["url"],
            "summary": translated_summary.strip()
        })

    return result


def format_slack_message(email_data, translated_articles):
    """Slack 메시지 포맷"""
    tldr_type = email_data["tldr_type"]
    emoji = TLDR_TYPE_EMOJI.get(tldr_type, "📬")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {tldr_type}",
                "emoji": True
            }
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"📅 {datetime.now().strftime('%Y-%m-%d')} • {len(translated_articles)}개 뉴스"
            }]
        },
        {"type": "divider"}
    ]

    current_section = ""

    for article in translated_articles[:12]:
        if article["section"] and article["section"] != current_section:
            current_section = article["section"]
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{current_section}*"
                }
            })

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<{article['url']}|{article['title']}>\n→ {article['summary']}"
            }
        })

    return {"blocks": blocks}


def send_to_slack(message):
    response = requests.post(
        SLACK_WEBHOOK_URL,
        json=message,
        headers={"Content-Type": "application/json"}
    )
    return response.status_code == 200


def main():
    print(f"[{datetime.now()}] TLDR 번역기 시작")

    # 환경 변수 확인
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DEEPL_API_KEY, SLACK_WEBHOOK_URL]):
        print("오류: 환경 변수가 설정되지 않음")
        print(f"  GMAIL_ADDRESS: {'✓' if GMAIL_ADDRESS else '✗'}")
        print(f"  GMAIL_APP_PASSWORD: {'✓' if GMAIL_APP_PASSWORD else '✗'}")
        print(f"  DEEPL_API_KEY: {'✓' if DEEPL_API_KEY else '✗'}")
        print(f"  SLACK_WEBHOOK_URL: {'✓' if SLACK_WEBHOOK_URL else '✗'}")
        return

    # Gmail 연결
    mail = connect_gmail()
    processed_ids = get_processed_ids()

    # TLDR 이메일 검색
    msg_ids = search_tldr_emails(mail, days_back=1)
    print(f"발견된 이메일: {len(msg_ids)}개")

    new_count = 0
    for msg_id in msg_ids:
        email_data = get_email_content(mail, msg_id)

        if not email_data:
            continue

        if email_data["id"] in processed_ids:
            continue

        print(f"\n처리 중: {email_data['tldr_type']} - {email_data['subject']}")

        links = extract_links(email_data["body"])
        articles = parse_articles(email_data["body"], links)
        print(f"  기사 수: {len(articles)}개")

        if not articles:
            print("  기사를 찾지 못함")
            continue

        translated = translate_articles(articles)
        message = format_slack_message(email_data, translated)

        if send_to_slack(message):
            print(f"  Slack 발송 성공!")
            processed_ids.add(email_data["id"])
            new_count += 1
        else:
            print(f"  Slack 발송 실패")

    mail.logout()
    save_processed_ids(processed_ids)
    print(f"\n[{datetime.now()}] 완료 - {new_count}개 발송")


if __name__ == "__main__":
    main()
