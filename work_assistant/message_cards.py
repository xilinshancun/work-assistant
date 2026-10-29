"""Bounded display-only cards and the readable projection returned by message GET.

GET omits URLs and visual styling. A matching projection verifies readable content,
not pixel layout or click-through behavior; callers retain the exact sent payload.
"""
import re
from .daily_import import http_link
from .util import strict_json


def card_projection(card):
    def fail():
        raise ValueError("unsupported display-only card structure")

    def text(value, markdown=False):
        if type(value) is not dict or set(value) != {"tag", "content"}:
            fail()
        if value["tag"] not in ({"plain_text", "lark_md"} if markdown else {"plain_text"}):
            fail()
        content = value["content"]
        if type(content) is not str or not content.strip() or len(content) > 5000 or re.search(r"https?://", content, re.I):
            fail()
        if value["tag"] == "lark_md":
            # Only balanced bold runs are supported by this small message entry.
            if content.count("**") % 2 or re.search(r"[<>\[\]`~_]", content):
                fail()
            content = content.replace("**", "")
        return content

    if type(card) is not dict or set(card) != {"config", "header", "elements"}:
        fail()
    conf, header = card["config"], card["header"]
    if (type(conf) is not dict or set(conf) - {"wide_screen_mode", "update_multi"}
            or any(type(v) is not bool for v in conf.values())):
        fail()
    if type(header) is not dict or set(header) != {"title", "template"} or header["template"] != "blue":
        fail()
    title = text(header["title"])
    if len(title) > 200 or type(card["elements"]) is not list or not 1 <= len(card["elements"]) <= 50:
        fail()
    rows = []
    for element in card["elements"]:
        if type(element) is not dict:
            fail()
        tag = element.get("tag")
        if tag == "div" and set(element) == {"tag", "text"}:
            rows.append([{"tag": "text", "text": text(element["text"], markdown=True).strip("\n")}])
        elif tag == "hr" and set(element) == {"tag"}:
            rows.append([{"tag": "hr"}])
        elif tag == "note" and set(element) == {"tag", "elements"} and type(element["elements"]) is list:
            rows.append([{"tag": "note", "elements": [{"tag": "text", "text": text(t)} for t in element["elements"]]}])
        elif tag == "action" and set(element) == {"tag", "actions"} and type(element["actions"]) is list:
            buttons = []
            if not 1 <= len(element["actions"]) <= 3:
                fail()
            for button in element["actions"]:
                if (type(button) is not dict or set(button) != {"tag", "text", "type", "url"}
                        or button["tag"] != "button" or button["type"] not in {"default", "primary", "danger"}
                        or type(button["url"]) is not str or not button["url"].startswith("https://")
                        or not http_link(button["url"])):
                    fail()
                buttons.append({"tag": "button", "text": text(button["text"]), "type": button["type"]})
            rows.append(buttons)
        else:
            fail()
    return {"title": title, "elements": rows}


def parse_card(raw):
    card = strict_json(raw)
    card_projection(card)
    return card
