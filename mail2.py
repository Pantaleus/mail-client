#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Terminálový e-mailový klient (IMAP/SMTP, Exchange-friendly).
Konfigurace se načítá z config.json.

autor: Pietro Dubsky
"""

import imaplib
import smtplib
import ssl
import sys
import re
import json
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import parseaddr, formataddr
from typing import List, Tuple, Optional

# ==========================
# Načtení config.json
# ==========================

with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

IMAP_HOST = CONFIG["imap"]["host"]
IMAP_PORT = CONFIG["imap"]["port"]
SMTP_HOST = CONFIG["smtp"]["host"]
SMTP_PORT = CONFIG["smtp"]["port"]
EMAIL_USER = CONFIG["auth"]["user"]
EMAIL_PASS = CONFIG["auth"]["password"]
FOLDERS = CONFIG["folders"]

# ==========================
# Nastavení
# ==========================

PAGE_SIZE = 20
FALLBACK_CHARSET = "utf-8"

# ==========================
# Pomocné funkce
# ==========================

def decode_maybe(value) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        try:
            return value.decode(FALLBACK_CHARSET, errors="replace") if isinstance(value, bytes) else str(value)
        except Exception:
            return str(value)

def extract_text_plain(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
                try:
                    return part.get_content()
                except Exception:
                    try:
                        return part.get_payload(decode=True).decode(part.get_content_charset() or FALLBACK_CHARSET, errors="replace")
                    except Exception:
                        continue
        return "(Žádná čitelná textová část.)"
    else:
        if msg.get_content_maintype() == "text":
            try:
                return msg.get_content()
            except Exception:
                try:
                    return msg.get_payload(decode=True).decode(msg.get_content_charset() or FALLBACK_CHARSET, errors="replace")
                except Exception:
                    return "(Nelze dekódovat tělo zprávy.)"
        return "(Zpráva není textového typu.)"

def input_nonempty(prompt: str) -> str:
    while True:
        s = input(prompt).strip()
        if s:
            return s
        print("Zadej prosím neprázdnou hodnotu.")

# ==========================
# IMAP
# ==========================

class ImapClient:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self):
        context = ssl.create_default_context()
        self.conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=context)
        typ, _ = self.conn.login(self.user, self.password)
        if typ != "OK":
            raise RuntimeError("IMAP přihlášení selhalo.")

    def close(self):
        try:
            if self.conn is not None:
                self.conn.logout()
        except Exception:
            pass

    def list_mailboxes(self) -> List[str]:
        """Vrátí seznam názvů složek (mailboxů) na serveru."""
        assert self.conn
        # Dovecot/ISPConfig potřebuje explicitně INBOX namespace
        typ, data = self.conn.list(pattern="*")
        if typ != "OK" or not data:
            return []
        boxes = []
        for line in data:
            try:
                decoded = line.decode("utf-8", errors="replace")
            except Exception:
                decoded = str(line)
            m = re.findall(r'"([^"]+)"', decoded)
            if m:
                name = m[-1]
            else:
                name = decoded.split()[-1].strip('"')
            if name and name != "." and name != '""':
                boxes.append(name)
        return sorted(set(boxes), key=str.lower)


    def select_mailbox(self, mailbox: str):
        assert self.conn
        typ, _ = self.conn.select(mailbox, readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Nelze vybrat schránku: {mailbox}")

    def search_uids(self, criteria: str = "ALL") -> List[bytes]:
        assert self.conn
        typ, data = self.conn.uid("SEARCH", None, criteria)
        if typ != "OK":
            return []
        ids = data[0].split() if data and data[0] else []
        return ids

    def fetch_headers(self, uid: bytes) -> Tuple[str, str, str]:
        assert self.conn
        typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return ("", "", "")
        hdr = message_from_bytes(data[0][1], policy=policy.default)
        return (
            decode_maybe(hdr.get("From")),
            decode_maybe(hdr.get("Subject")),
            decode_maybe(hdr.get("Date")),
        )

    def fetch_full(self, uid: bytes) -> EmailMessage:
        assert self.conn
        typ, data = self.conn.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError("Nelze načíst zprávu.")
        return message_from_bytes(data[0][1], policy=policy.default)

    def append_message(self, mailbox: str, msg: EmailMessage):
        assert self.conn
        self.conn.append(mailbox, None, None, msg.as_bytes())

# ==========================
# SMTP
# ==========================

class SmtpClient:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.conn: Optional[smtplib.SMTP] = None

    def connect(self):
        context = ssl.create_default_context()
        try:
            if CONFIG["smtp"]["ssl"]:
                print("SMTP: pokus o připojení na port 465 (SSL)…")
                self.conn = smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=20)
                self.conn.login(self.user, self.password)
                print("SMTP: připojení přes SSL OK.")
            else:
                raise Exception("SSL není povoleno v configu")
        except Exception as e:
            if CONFIG["smtp"].get("starttls_fallback", False):
                print(f"SMTP SSL selhalo: {e}")
                print("SMTP: zkouším STARTTLS na portu 587…")
                self.conn = smtplib.SMTP(self.host, 587, timeout=20)
                self.conn.ehlo()
                self.conn.starttls(context=context)
                self.conn.login(self.user, self.password)
                print("SMTP: připojení přes STARTTLS OK.")
            else:
                raise

    def close(self):
        try:
            if self.conn is not None:
                self.conn.quit()
        except Exception:
            pass

    def send_text(self, to_addrs: List[str], subject: str, body: str,
                  from_name: Optional[str] = None, imap_client: Optional['ImapClient'] = None):
        msg = EmailMessage()
        sender_display = formataddr((from_name or self.user, self.user))
        msg["From"] = sender_display
        msg["To"] = ", ".join(to_addrs)
        msg["Subject"] = subject
        msg.set_content(body)

        # 1) Odeslání přes SMTP
        self.conn.send_message(msg)
        print("SMTP: zpráva odeslána.")

        # 2) Uložení kopie do Sent (pokud je k dispozici IMAP klient)
        if imap_client:
            try:
                sent_box = FOLDERS["sent"]  # bereme přímo z configu
                print(f"IMAP: pokus o zápis do '{sent_box}'…")
                imap_client.select_mailbox(sent_box)
                imap_client.conn.append(sent_box, None, None, msg.as_bytes())
                print(f"IMAP: zpráva zapsána do složky '{sent_box}'.")
            except Exception as e:
                print(f"IMAP: nepodařilo se uložit do Sent ({e}).")

# ==========================
# UI / APLIKACE
# ==========================

class MailApp:
    def __init__(self):
        self.imap = ImapClient(IMAP_HOST, IMAP_PORT, EMAIL_USER, EMAIL_PASS)
        self.smtp = SmtpClient(SMTP_HOST, SMTP_PORT, EMAIL_USER, EMAIL_PASS)

    def start(self, debug_folders: bool = False):
        print("Připojuji IMAP/SMTP…")
        self.imap.connect()
        self.smtp.connect()
        print("Hotovo. Přihlášen jako:", EMAIL_USER)

        if debug_folders:
            print("=== DEBUG: LIST ===")
            typ, data = self.imap.conn.list("", "*")
            print('LIST "" "*" =>', typ)
            if data:
                for line in data:
                    print(" ", line.decode(errors="replace"))

        print("\nDostupné složky:")
        for box in self.imap.list_mailboxes():
            print(" -", box)

        self.main_menu()

    def main_menu(self):
        while True:
            print("\n=== MENU ===")
            print("1) Načíst a procházet INBOX")
            print("2) Procházet Odeslanou poštu (Sent)")
            print("3) Procházet Koš (Trash)")
            print("4) Procházet Spam")
            print("5) Odeslat nový e-mail")
            print("0) Konec")
            choice = input("Volba: ").strip()

            try:
                if choice == "1":
                    self.browse_folder(FOLDERS["inbox"])
                elif choice == "2":
                    self.browse_folder(FOLDERS["sent"])
                elif choice == "3":
                    self.browse_folder(FOLDERS["trash"])
                elif choice == "4":
                    self.browse_folder(FOLDERS["spam"])
                elif choice == "5":
                    self.compose_and_send()
                elif choice == "0":
                    print("Odpojuji…")
                    self.shutdown()
                    return
                else:
                    print("Neplatná volba.")
            except Exception as e:
                print(f"[CHYBA] {e}")

    def browse_folder(self, mailbox: str):
        print(f"\n>>> Schránka: {mailbox}")
        self.imap.select_mailbox(mailbox)
        uids = self.imap.search_uids("ALL")
        if not uids:
            print("Žádné zprávy.")
            return
        uids = uids[::-1]

        for idx, uid in enumerate(uids[:PAGE_SIZE], start=1):
            From, Subject, Date = self.imap.fetch_headers(uid)
            print(f"{idx}) {Date} | {decode_maybe(From)} | {decode_maybe(Subject)}")

        cmd = input("Číslo zprávy = otevřít, Enter = zpět: ").strip()
        if cmd.isdigit():
            sel = int(cmd)
            if 1 <= sel <= len(uids[:PAGE_SIZE]):
                self.open_message(mailbox, uids[sel-1])

    def open_message(self, mailbox: str, uid: bytes):
        msg = self.imap.fetch_full(uid)
        From = decode_maybe(msg.get("From"))
        To = decode_maybe(msg.get("To"))
        Subject = decode_maybe(msg.get("Subject"))
        Date = decode_maybe(msg.get("Date"))

        print("\n==================== ZPRÁVA ====================")
        print(f"From:    {From}")
        print(f"To:      {To}")
        print(f"Subject: {Subject}")
        print(f"Date:    {Date}")
        print("------------------------------------------------")
        print(extract_text_plain(msg))
        print("================================================\n")

        while True:
            cmd = input("[o]dpovědět | [f]orward | [b]ack: ").strip().lower()
            if cmd == "o":
                self.reply_to_message(msg)
                break
            elif cmd == "f":
                self.forward_message(msg)
                break
            elif cmd == "b":
                break

    def compose_and_send(self):
        to_line = input_nonempty("Komu (odděluj čárkami): ")
        subject = input("Předmět: ").strip()
        print("Text zprávy (ukonči na prázdném řádku s tečkou '.'):")
        lines = []
        while True:
            line = input()
            if line.strip() == ".":
                break
            lines.append(line)
        body = "\n".join(lines)
        to_addrs = [a.strip() for a in to_line.split(",") if a.strip()]
        self.smtp.send_text(to_addrs, subject, body, imap_client=self.imap)

    def reply_to_message(self, original: EmailMessage):
        orig_from = parseaddr(original.get("From"))[1]
        subject = original.get("Subject") or ""
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        print(f"Odpověď na: {orig_from}")
        print("Text zprávy (ukonči na prázdném řádku s tečkou '.'):")
        lines = []
        while True:
            line = input()
            if line.strip() == ".":
                break
            lines.append(line)
        body = "\n".join(lines)
        self.smtp.send_text([orig_from], subject, body, imap_client=self.imap)

    def forward_message(self, original: EmailMessage):
        to_line = input_nonempty("Přeposlat komu (čárkami): ")
        subject = original.get("Subject") or ""
        if not subject.lower().startswith("fw:"):
            subject = "Fwd: " + subject
        intro = [
            "---------- Původní zpráva ----------",
            f"From: {decode_maybe(original.get('From'))}",
            f"Date: {decode_maybe(original.get('Date'))}",
            f"Subject: {decode_maybe(original.get('Subject'))}",
            f"To: {decode_maybe(original.get('To'))}"
        ]
        orig_body = extract_text_plain(original)
        composed = "\n".join(intro) + "\n\n" + orig_body
        to_addrs = [a.strip() for a in to_line.split(",") if a.strip()]
        self.smtp.send_text(to_addrs, subject, composed, imap_client=self.imap)

    def shutdown(self):
        self.smtp.close()
        self.imap.close()

def main():
    app = MailApp()
    try:
        app.start()
    except KeyboardInterrupt:
        print("\nUkončeno uživatelem.")
        app.shutdown()
    except Exception as e:
        print(f"[FATAL] {e}")
        try:
            app.shutdown()
        except Exception:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
