#!/usr/bin/env python3
"""Вырезание ключей и токенов из текста для collect_diag.sh. Python 3.8+, только стандартная библиотека.

  redact.py              stdin → stdout построчно: журналы, вывод команд, YAML, env, юниты systemd, код, XML;
  redact.py --json       stdin — JSON целиком: разбор и обход по ключам (не разобрался — построчно);
  redact.py --check DIR  повторный проход по всем файлам каталога: найденное вырезается на месте,
                         в stdout — исправленные файлы (ожидается пусто).

Секретом считаются:
  * значения узнаваемого вида где угодно: sk-…, Bearer/Basic …, user:pass@ и длинный токен перед @ в адресе,
    JWT, токены VK (vk1.a.…, 85 hex), GitHub, GitLab, Slack, Telegram (и в пути …/bot<токен>/), AWS, Google,
    блоки PRIVATE KEY; пароль в curl -u user:пароль, sshpass -p, «echo пароль | sudo -S» и «sudo -S <<< пароль»,
    пароль Wi-Fi в nmcli (password V, wifi-sec.psk V) и wpa_passphrase SSID пароль;
  * значение, присвоенное секретному имени — имени, в котором есть api_key, key, token, secret, pass, password,
    pwd, auth, authorization, credential, cookie, psk и т. п. Присваивание узнаётся по форме, а не по языку:
      - имя, разделитель, значение: NAME=v, NAME: v, NAME := v, NAME ?= v, NAME => v, параметр ?NAME=v в URL
        (и в URL-кодировке: NAME%3Dv); имя бывает в кавычках (и в экранированных — JSON внутри строки JSON),
        под индексом (environ["NAME"] = v), через точку (cfg.token = v), среди нескольких целей
        (TOKEN, CHAT_ID = v, 1), с аннотацией типа (def f(token: str = v)), за словом объявления (const, local),
        поле структуры (Key: v,); значение — строка (и склейка строк: "a" "b"), число, скобки, лямбда с телом
        или выражение целиком (os.getenv(…) or "…"), у оператора присваивания — и через запятую (API_KEYS=a,b;
        TOKEN = "a", "b"); строка, с которой имя сравнивается (== "…"), тоже секрет;
      - имя в кавычках в списке аргументов: остаток списка (setdefault("NAME", v), ("NAME", v));
      - запасное значение: os.environ.get("NAME") or v, token or "v", token if token else "v";
      - пара «имя — значение»: name="NAME" … value="v" (XML, аргументы, JSON, элемент YAML), <NAME>v</NAME>
        и <param name="NAME">v</param>;
      - флаг: --NAME v, --NAME=v, --NAME a,b;
      - значение на следующих строках, в том числе как переносит black внутри скобок:
          после «\» (и когда перенос до или после оператора: «v \» ↵ «or "w"»);
          у оператора без «\»: оператор в конце строки («"a" +» ↵ «"w"») или в начале следующей, если
          выражение за ним кончается разделителем или скобкой («api_key=v» ↵ «or "w",», «if c» ↵ «else "w"»),
          и строка за строкой — склейка («api_key="a"» ↵ «"w",»); пустая строка и комментарий между ними не мешают;
          список аргументов по одному на строке за именем в кавычках («"--token",» ↵ «default="w",»);
          имя в кавычках одно на строке внутри вызова или индекса («"NAME"» ↵ «] = "w"», «) or "w"»);
          после ключа без значения и блок YAML;
          незакрытая скобка — до закрытия, и когда она открыта за первым операндом («''.join(»), а за «]» и «=» —
          значение после «=» («api_key: Optional[» ↵ «str» ↵ «] = "w"»);
          строка в тройных кавычках или с «\» в конце — до закрывающей кавычки.
        От многострочного значения остаются скобки, знаки препинания, операторы и литералы.
    У Authorization и Cookie без кавычек значение берётся целиком, со схемой (token …, OAuth …). Числа тоже:
    пароль бывает из одних цифр.
Не трогаются true/false/null и пустые значения, пределы max_* и числа у счётчиков токенов
(summarize_token_percent, default_token_budget): «max_tokens: 1024» остаётся читаемым. Лишнее вырезание
(например, «tokenizer: o200k», «sort_key: name») допустимо, пропуск секрета — нет.
"""
import json
import os
import re
import sys

MASK = "<redacted>"

# Короткие слова (key, pass, auth, psk, pwd, psw, pswd) — только целым словом: после не-буквы или на «горбе»
# camelCase (authKey, sudoPass) и не перед буквой, чтобы не задеть monkey, bypass, author, KeyError, PassEnvironment.
_START = r"(?:(?<![a-z0-9])|(?-i:(?<=[a-z0-9])(?=[A-Z])))"
_WORD = (r"(?:api[_-]?keys?|apikey|access[_-]?keys?|(?:private|encrypt|aes|signing)[_-]?key|secret|token"
         r"|passw(?:or)?d|passphrase|sshpass|authorization|credentials?|cookie"
         r"|" + _START + r"(?:keys?|pass|auth|psk|pwd|pswd|psw)(?![a-z]))")
_SECRET_NAME = re.compile(r"[A-Za-z0-9_.-]{0,64}" + _WORD + r"[A-Za-z0-9_.-]{0,64}", re.I)
# Имя-кандидат в шаблонах — просто знаки имени (секрет ли оно, решает secret_name): так регулярные выражения
# компилируются быстро, а redact.py запускается на каждую команду сбора. Длина ограничена: иначе на длинной строке
# из букв и цифр (hex, base64url) поиск квадратичный.
_NAME_RUN = r"[A-Za-z0-9_.-]{1,160}"
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_LIMIT = re.compile(r"^-*max[_-]", re.I)  # max_tokens, --max-tokens — пределы, а не секреты
# Счётчики токенов (summarize_token_percent, default_token_budget): число под таким именем — не секрет.
_COUNTER = re.compile(r"tokens|token[_-]", re.I)
_STRICT = re.compile(r"passw|secret|passphrase|credential|cookie|key|auth", re.I)
# Пара «имя — значение»: поле-имя называет секрет, значение лежит в соседнем поле.
_PAIR_KEYS = ("name", "key", "id", "var")
_PAIR_VALUE = r"(?<![\w.-])(?:value|default(?:_value)?|val)"
_PAIR_VALUE_NAME = re.compile(r"^" + _PAIR_VALUE + r"$", re.I)
_PAIR = re.compile(r"""(?<![\w.-])(?:name|key|id|var)(?:\\*["'])?[ \t]*[:=][ \t]*(?:\\*["'])?(?P<v>"""
                   + _NAME_RUN + r""")(?![\w.-])""", re.I)
# Пара в YAML, многострочном вызове или объекте: «- name: NAME», значение — в соседнем поле ниже на том же отступе.
_PAIR_ITEM = re.compile(r"""^(?P<ind>[ \t]*(?:-[ \t]+|[{(][ \t]*)?)(?P<k>["']?)(?:name|key|id|var)(?P=k)"""
                        r"""[ \t]*[:=][ \t]*(?P<q>["']?)(?P<v>""" + _NAME_RUN + r""")(?P=q)[ \t]*,?[ \t]*(?:#.*)?$""",
                        re.I)
_SECRET_VALUE = r"""(?:<redacted>|"[^"]*"|'[^']*'|[^\s"'<]+)"""  # маска — тоже значение: повтор ничего не меняет


def secret_name(name):
    return bool(_SECRET_NAME.fullmatch(name)) and not _LIMIT.match(name)


def _url_param(m):
    """Параметр URL с секретным именем (и подпись sig=): значение до «&» (long poll VK: key=)."""
    name = m.group(2)
    return m.group(1) + MASK if name.lower() in ("sig", "signature") or secret_name(name) else m.group(0)


def _xml_element(m):
    """Элемент XML, у которого секрет называет имя или атрибут name/key: <token>v</token>,
    <param name="api_key">v</param>."""
    secret = secret_name(m.group(2)) or any(secret_name(a.group("v")) for a in _PAIR.finditer(m.group(1)))
    return m.group(1) + MASK + m.group(3) if secret else m.group(0)


# (подстрока в строке в нижнем регистре, без которой шаблон не сработает; шаблон; замена). Подстрока — быстрая
# проверка перед регулярным выражением: журнал в несколько МБ на Jetson проходится заметно быстрее.
PATTERNS = [
    ("private key", re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(-----END [A-Z ]*PRIVATE KEY-----)"),
     r"\1" + MASK + r"\2"),
    ("sk-", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), MASK),  # OpenAI-совместимые ключи, в т. ч. RouterAI
    ("_", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), MASK),
    ("glpat-", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), MASK),
    ("xox", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    ("akia", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    ("aiza", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), MASK),
    ("eyj", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),  # JWT
    (":", re.compile(r"(?:\b|(?<=bot))\d{6,12}:[A-Za-z0-9_-]{30,}"), MASK),  # токен бота Telegram, и в …/bot<токен>/
    ("vk1.a.", re.compile(r"\bvk1\.a\.[A-Za-z0-9_-]{20,}"), MASK),  # токен VK
    ("", re.compile(r"\b[0-9a-f]{85}\b"), MASK),  # токен VK прежнего вида
    ("=", re.compile(r"([?&](" + _NAME_RUN + r")=)[^&#\s\"'<>]+"), _url_param),  # ?access_token=v&
    ("</", re.compile(r"(<(" + _NAME_RUN + r")(?:[ \t][^<>]*)?>)[^<]+(</\2>)"), _xml_element),  # <token>v</token>
    ("bearer", re.compile(r"\b(bearer)(\s+)[A-Za-z0-9._~+/-]{6,}=*", re.I), r"\1\2" + MASK),
    ("basic", re.compile(r"\b(Basic)(\s+)(?=[A-Za-z0-9+/]*[0-9+/])[A-Za-z0-9+/]{8,}={0,2}"), r"\1\2" + MASK),
    ("://", re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I), r"\1" + MASK + "@"),
    # https://<токен>@github.com
    ("://", re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]{16,}@", re.I), r"\1" + MASK + "@"),
    ("-u", re.compile(r"""((?<![\w-])(?:-u|-U|--user|--proxy-user)(?:[ \t]+|=)["']?[^\s:"']*:)[^\s"']+"""),
     r"\1" + MASK),  # curl -u user:пароль
    ("sshpass", re.compile(r"(\bsshpass\b[^\n|;&]*?[ \t]-p[ \t]*)" + _SECRET_VALUE), r"\1" + MASK),
    ("|", re.compile(r"(\b(?:echo|printf)[ \t]+)[^|\n]*?"
                     r"(?=[ \t]*\|[ \t]*(?:sudo\b[^|\n]*?[ \t]-[A-Za-z]*S|(?:sudo[ \t]+)?chpasswd\b))"),
     r"\1" + MASK),  # echo пароль | sudo -S, echo user:пароль | chpasswd
    ("<<<", re.compile(r"(\bsudo\b[^\n|;&]*?[ \t]-[A-Za-z]*S\b[^\n|;&]*?<<<[ \t]*)" + _SECRET_VALUE), r"\1" + MASK),
    # Wi-Fi: у nmcli значение свойства — через пробел (password V, wifi-sec.psk V); wpa_passphrase SSID пароль
    ("nmcli", re.compile(r"(\bnmcli\b[^\n|;&]*?[ \t][\w.-]*(?:password|psk|secret)[\w.-]*[ \t]+)" + _SECRET_VALUE,
                         re.I), r"\1" + MASK),
    ("wpa_passphrase", re.compile(r"""(\bwpa_passphrase[ \t]+(?:"[^"]*"|'[^']*'|[^\s"']+)[ \t]+)""" + _SECRET_VALUE),
     r"\1" + MASK),
]


# Присваивание: имя и разделитель; значение разбирается отдельно (_value). Имя бывает в кавычках, в том числе
# экранированных (JSON внутри строки JSON: \"api_key\":\"…\"), под индексом (environ["NAME"] = …) и в вызове
# перед запасным значением (get("NAME") or …; get("NAME") в конце строки — «or» строкой ниже). Запятая после имени
# в кавычках — аргументы вызова: setdefault("NAME", …); после имени без кавычек — несколько целей присваивания
# (TOKEN, CHAT_ID = …). «==», «!=» — сравнение: секрет только строка в кавычках справа. Пробелы у «=» (sp) отличают
# «password = a b c» (INI, код) от «TOKEN=abc python3 run.py» (shell). Имена value/default — поле-значение пары,
# секрет только в строке пары.
_HEAD = re.compile(r"""(?P<q>(?:\\*["'])?)(?P<name>""" + _NAME_RUN + r""")(?P=q)(?P<close>[\])]?)"""
                   r"""(?P<sp>[ \t]*)(?P<sep>=>|:=|[?+]=|[!=]==?|:(?!:)|=|,|(?<=[ \t])(?-i:or|if)(?=[ \t]|$)|$)"""
                   r"""(?P<sp2>[ \t]*)""", re.I)
# Поиск присваиваний идёт от опорных подстрок секретных слов (_starts), а не с каждой позиции строки: в каждом
# слове _WORD есть одна из них.
_ANCHORS = ("key", "secret", "token", "pass", "pwd", "psw", "auth", "credential", "cookie", "psk")
_PAIR_VALUE_RX = re.compile(_PAIR_VALUE + r"(?![\w.-])", re.I)
# отступ и «- » элемента списка YAML или слово объявления (const token = …: как в начале строки)
_LEAD = re.compile(r"[ \t]*(?:-[ \t]*|(?:const|let|var|export|local|readonly)[ \t]+)?")
# URL-кодировка (api_key%3Dv, %22token%22%3A%22v%22): разделитель, имя перед ним, значение до %22, %26, %2C, %7D.
_URL_SEP = re.compile(r"(?:%2[27])?%3[AD](?:%2[27])?", re.I)
_URL_VALUE = re.compile(r"(?:[^&\s\"'%<>]|%(?!2[267C]|7D)[0-9A-F]{2})+", re.I)
_FLAG = re.compile(r"(?<![\w-])(--?" + _NAME_RUN + r")[ \t]+(?=[^\s-])")
_LITERAL = re.compile(r"""^((?:\\*["'])?)(?:true|false|null|none|yes|no|~|<redacted>|)\1$""", re.I)
_NUMBER = re.compile(r"""^((?:\\*["'])?)-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\1$""")
# Начало строки в кавычках: префикс f/r/b/u или $ (bash $'…'), тройные кавычки, экранированная кавычка.
_QUOTE = re.compile(r"(?:\$|[rRbBuUfF]{0,2})(\"{3}|'{3}|\\*[\"'])")
_GLUED = re.compile(r"[ \t]+(?=" + _QUOTE.pattern + ")")  # склейка строк через пробел: "a" "b", "a" f"{b}"
# продолжение выражения в коде: +, %, or, and, if/else, in, is, not (и в конце строки: «x or» ↵ «"v"»)
_OPERATOR = re.compile(r"[ \t]*(?:[+%]|(?<=[ \t])(?:or|and|if|else|in|is|not)(?=[ \t]|$))"
                       r"(?:[ \t]+not(?=[ \t]))?[ \t]*")
# операторы в начале строки-продолжения: «    or "v"», «    if not c»
_LEAD_OP = re.compile(r"(?:(?:[+%]|(?:or|and|if|else|in|is|not)(?=[ \t]))[ \t]*)*")
_LINE_OP = re.compile(r"([+%]|or|and|if|else|in|is|not)[ \t]+")  # строка с оператора продолжает значение выше
_ELSE = re.compile(r"[ \t]else[ \t]")
_REST_VALUE = re.compile(r"[ \t]*(?:[,;)\]}#\\]|$)")  # за продолжением значения: разделитель, скобка, конец строки
# Оператор в конце значения до конца строки (INI, shell, код без скобок): «token = x or» ↵ «"v"».
_OP_END = re.compile(r"(?<=[ \t])(?:[+%]|or|and|if|else|in|is|not)$")
# Значение оператора присваивания продолжается через запятую (TOKEN = "a", "b"; API_KEYS=a,b), если за ней не
# следующая пара «имя = …» (api_key="v", timeout=5 в многострочном вызове).
_PAIR_NEXT = r"""(?:\\*["'])?[A-Za-z_][\w.-]*(?:\\*["'])?[ \t]*(?:=>|[:+?]?=(?!=))"""
_COMMA_OP = re.compile(r"[ \t]*,(?![ \t]*" + _PAIR_NEXT + r")[ \t]*")
_COMMA_GLUE = re.compile(r",(?=[^\s,;)\]}])")  # запятая внутри слова: API_KEYS=a,b
# Строка после «\» за уже взятым значением — не продолжение, если это новая пара или флаг (ENV A=1 \ B=2).
_NEW_ITEM = re.compile(r"-|" + _PAIR_NEXT)
_ASSIGN = re.compile(r"[ \t]*[-+*/|&%^]?=(?![=>])[ \t]*")  # оператор присваивания после аннотации типа
_KWARG = re.compile(r"[A-Za-z_]\w*[ \t]*=(?![=>])[ \t]*")  # именованный аргумент на своей строке: default=
_ARG_COMMA = re.compile(r"[ \t]*,[ \t]*(?:#.*)?$")  # запятая в конце строки: список аргументов идёт дальше
# заголовок лямбды: lambda:, lambda x, y=1:, lambda s="a": (параметры ограничены по длине)
_LAMBDA = re.compile(r"""lambda(?:[ \t]+(?:[\w \t,*=.]|"[^"\n]{0,32}"|'[^'\n]{0,32}'){0,64})?:[ \t]*""")
# Аннотация типа с умолчанием в строке def (str =, Optional[str] =, "str" =) и литерал в ветке else у запасного
# значения (token if token else "v"). Длина ограничена: оба проверяются от каждого найденного имени.
_ANNOTATED = re.compile(r"""(?:[\w.]{1,64}(?:\[[^=\n]{0,128}?\])?|"[^"\n]{0,64}"|'[^'\n]{0,64}')"""
                        r"""[ \t]*=(?!=)[ \t]*""")
_ELSE_LITERAL = re.compile(r"""[^\n]{0,160}?[ \t]else[ \t]+[rRbBuUfF]{0,2}["']""")
# Несколько целей присваивания (TOKEN, CHAT_ID = …): их число и длина ограничены, иначе строка из многих «token, …»
# проходится за квадратичное время.
_TARGET = r"\*?[A-Za-z_][\w.]{0,63}(?:\[[^\]\n]{0,64}\])?"
_TARGETS = re.compile(r"(?:" + _TARGET + r"[ \t]*,[ \t]*){0,8}(?:" + _TARGET + r")?[ \t]*:?=(?!=)[ \t]*")
# «:» в конце заголовка блока Python (if token: …) — не присваивание; в строке def — аннотация типа.
_BLOCK_LINE = re.compile(r"[ \t]*(?:(?:el)?if|else|while|for|with|try|except|finally|async[ \t]+(?:for|with))\b")
_DEF_LINE = re.compile(r"[ \t]*(?:async[ \t]+)?def[ \t]")
_SPACED = re.compile(r"(?:authorization|cookie)$", re.I)  # значение заголовка — со схемой и пробелами
_HEADER_VALUE = re.compile(r"""(?:[^"'\r\n\\]|\\(?![rn]))*""")  # до кавычки или до \r, \n (repr запроса)
_REST_BLANK = re.compile(r"[ \t]*$")
_REST_COMMENT = re.compile(r"[ \t]*#")
_REST_EOL = re.compile(r"[ \t]*(?:#.*)?$")  # до конца строки — пусто или комментарий
_REST_CONT = re.compile(r"[ \t]*\\[ \t]*$")  # «\» в конце строки: продолжение на следующей
_REST_END = re.compile(r"[ \t]*[,;]?[ \t]*(?:#.*|\\[ \t]*)?$")  # за значением строки YAML, INI: конец строки
_ALL_MASKED = re.compile(r"""^\s*(?:(["']?)<redacted>\1\s*,?\s*)*$""")
_BLOCK_SCALAR = re.compile(r"[|>][-+0-9]*\s*(?:#.*)?$")
# Открытое значение (_walk): слово — до пробела, знака структуры или кавычки (кавычка открывает строку, как
# в _outside: иначе повторный проход разобрал бы вырезанное иначе); литералы и операторы (or, in, +, %) не
# прячутся: повторный проход связывает операнды выражения так же, как первый.
_RUN = re.compile(r"""[^\s,;:=()\[\]{}"']+""")
_KEEP = re.compile(r"(?:true|false|null|none|yes|no|~|or|and|not|if|else|in|is|[-+*/%])$", re.I)
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def _counter(name):
    return bool(_COUNTER.search(name)) and not _STRICT.search(name)


def _plain(value, name):
    """Значение под секретным именем, которое не прячется: пустое, true/false/null, маска; число — у счётчика."""
    return bool(_LITERAL.match(value)) or (_counter(name) and bool(_NUMBER.match(value)))


def _outside(text, start=0):
    """Позиции и знаки вне строк в кавычках, начиная с позиции start."""
    quote, i = None, start
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        else:
            yield i, c
        i += 1


class _Depth:
    """Вложенность скобок вне строк в кавычках на позициях одной строки. Позиции спрашиваются по возрастанию, и строка
    проходится один раз: длинная строка со многими присваиваниями — за линейное время."""

    def __init__(self, s):
        self.s, self.i, self.depth, self.quote = s, 0, 0, None

    def at(self, j):
        if j < self.i:
            self.i, self.depth, self.quote = 0, 0, None
        while self.i < j:
            c = self.s[self.i]
            if self.quote:
                if c == "\\":
                    self.i += 1
                elif c == self.quote:
                    self.quote = None
            elif c in "\"'":
                self.quote = c
            elif c in "([{":
                self.depth += 1
            elif c in ")]}":
                self.depth -= 1
            self.i += 1
        return self.depth


def _close(s, i):
    """Позиция скобки, закрывающей открытую на позиции i, или None."""
    depth = 0
    for j, c in _outside(s, i):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if not depth:
                return j
    return None


def _trim(s, i, j, chars=" \t"):
    """Конец отрезка s[i:j] без знаков chars в конце."""
    while j > i and s[j - 1] in chars:
        j -= 1
    return j


def _quoted_end(s, start, q):
    """Позиция после строки в кавычках q, открытой до позиции start, или None. У обычной кавычки \\x внутри —
    экранированный знак, '' внутри '…' — апостроф (YAML); у экранированной (\\" — JSON внутри строки JSON)
    строку закрывает такая же кавычка с тем же числом обратных косых перед ней."""
    if q in ('"""', "'''"):
        end = s.find(q, start)
        return None if end < 0 else end + 3
    k, c, i = len(q) - 1, q[-1], start
    while i < len(s):
        if not k and s[i] == "\\":
            i += 2
        elif not k and c == "'" and s.startswith("''", i):
            i += 2
        elif s[i] == c and (not k or _backslashes(s, i) == k):
            return i + 1
        else:
            i += 1
    return None


def _backslashes(s, i):
    """Сколько обратных косых стоит подряд перед позицией i."""
    j = i
    while j and s[j - 1] == "\\":
        j -= 1
    return i - j


def _walk(s, i, depth, quote, stop):
    """Открытое значение (скобка или строка не закрыта на строке) с позиции i: остаются пробелы, скобки, знаки
    , ; : =, кавычки, литералы и слова-операторы (or, in …); содержимое строк, остальные слова и комментарий —
    маска. depth и quote — незакрытые скобки и строка с прошлых строк. Возвращает текст; позицию за скобкой или
    кавычкой, которая закрыла значение (только при stop, иначе None); вложенность и незакрытую кавычку в конце.
    На следующую строку переносится только строка в тройных кавычках или с «\» в конце: незакрытая обычная кавычка
    чаще значит вложенные кавычки ('k="v"' в коде тестов и журналах), и перенос сдвинул бы разбор следующих строк
    на одну кавычку — тогда значение вместе со скобками кончается на этой строке."""
    out, n = [], len(s)
    while i < n:
        if quote:
            end = _quoted_end(s, i, quote)
            close = n if end is None else end - len(quote)
            out.append(MASK if s[i:close].strip() else s[i:close])
            if end is None:
                if quote not in ('"""', "'''") and not s.endswith("\\"):
                    return "".join(out), None, 0, None
                break
            out.append(s[close:end])
            i, quote = end, None
            if stop and not depth:
                return "".join(out), i, 0, None
            continue
        c = s[i]
        if c.isspace() or c in ",;:=":
            out.append(c)
            i += 1
        elif c in "([{":
            out.append(c)
            depth, i = depth + 1, i + 1
        elif c in ")]}":
            out.append(c)
            depth, i = max(depth - 1, 0), i + 1
            if stop and not depth:
                return "".join(out), i, 0, None
        elif s.startswith(MASK, i):
            out.append(MASK)
            i += len(MASK)
        elif c == "#":  # комментарий — до конца строки (в нём бывает прежний ключ)
            text = s[i + 1:].lstrip(" \t")
            out.append(s[i:n - len(text)] + (MASK if text else ""))
            break
        else:
            m = _QUOTE.match(s, i)
            if m:
                out.append(s[i:m.end()])
                i, quote = m.end(), m.group(1)
            else:
                j = _RUN.match(s, i).end()
                out.append(s[i:j] if _KEEP.match(s[i:j]) else MASK)
                i = j
    return "".join(out), None, depth, quote


def _open(s, i):
    """Открытое значение от позиции i до конца строки: текст с маской и блок для следующих строк (None — закрылось)."""
    text, _, depth, quote = _walk(s, i, 0, None, False)
    return text, _open_block(depth, quote)


def _open_block(depth, quote):
    return ("open", depth, quote) if depth or quote else None


def _opened(text):
    return _open(text, 0)[1] is not None


def _args_end(s, i):
    """Конец остатка списка аргументов от позиции i — до закрывающей скобки без пары или до « #комментария» — и вид
    продолжения, если скобка на строке не закрылась: "args" — остаток пуст, список — ниже, по аргументу на строке,
    как переносит black (add_argument( ↵ "--token", ↵ default="v",); "kwargs" — остаток кончается запятой (она
    остаётся видна), ниже — именованные аргументы; "open" — в остатке не закрыты скобка или строка."""
    depth = 0
    for j, c in _outside(s, i):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return _trim(s, i, j), None
            depth -= 1
    if _REST_EOL.match(s, i):
        return i, "args"
    end = _trim(s, i, _comment(s, i))
    if _opened(s[i:end]):
        return end, "open"
    if s[end - 1] == ",":
        return _trim(s, i, end, " \t,"), "kwargs"
    return end, None


def _piece_end(s, i):
    """Конец слова от позиции i и вид продолжения ("open" — скобка или строка не закрыты, значение продолжается
    ниже). Слово — с вызовами и индексами (os.environ.get("X"), cfg["a"]) или строка в кавычках со склейкой
    вплотную и через пробел ("a"'b', "a" "b", "a".strip()). Кавычка посреди слова, перед которой не строка, закрывает
    внешнюю строку: у "TOKEN=abc" "MODE=1" значение — abc. Уже вырезанное значение — маска целиком, вместе с вызовом
    или индексом за ней: на повторном проходе <redacted>( и "a"<redacted>( (вырезанный "a".format()) — то же слово
    и та же незакрытая скобка, что и на первом. Незакрытая строка — значение до конца строки (и ниже, см. _walk),
    если с неё начинается слово или она в тройных кавычках; иначе слово кончается перед ней: кавычка за строкой,
    которая не закрывается, — обычно конец внешней строки ('k="v"' в коде тестов и журналах)."""
    start, n = i, len(s)
    if s.startswith(MASK, i):
        i += len(MASK)
        if s[i:i + 1] not in ("(", "[", "."):
            return i, None
    while i < n and not s[i].isspace() and s[i] not in ",;)]}":
        m = _QUOTE.match(s, i) if i == start or s[i - 1] in "\"' \t" else None
        if m:
            end = _quoted_end(s, m.end(), m.group(1))
            if end is None:
                if i == start or m.group(1) in ('"""', "'''"):
                    return n, "open"
                return _trim(s, start, i), None
            i = end
            glued = _GLUED.match(s, i)
            if glued:
                i = glued.end()
            elif i < n and not (s[i] in "\"'.([_" or s[i].isalnum() or s.startswith(MASK, i)):
                break
        elif s[i] in "\"'":
            break
        elif s[i] in "([{":
            close = _close(s, i)
            if close is None:
                return n, "open"
            i = close + 1
        else:
            i += 1
    return i, None


def _operand_end(s, i, commas=False):
    """Конец операнда от позиции i и вид продолжения (см. _piece_end); у лямбды операнд — её тело
    (key=lambda: os.getenv("T") or "v"). commas — значение оператора присваивания: запятая без пробела после неё —
    часть слова (API_KEYS=a,b)."""
    lam = _LAMBDA.match(s, i)
    if lam:
        i = lam.end()
    while True:
        i, kind = _piece_end(s, i)
        if kind or not commas or not _COMMA_GLUE.match(s, i):
            return i, kind
        i += 1


def _expr_end(s, i, commas=False):
    """Конец значения-выражения от позиции i и вид продолжения: операнды через +, or, and, if/else, in
    (token = os.getenv("X") or "…"); при commas — и через запятую (TOKEN = "a", "b"), кроме следующей пары."""
    end, kind = _operand_end(s, i, commas)
    return (end, kind) if kind or end == i else _expr_tail(s, end, commas)


def _expr_tail(s, end, commas=False):
    """Операнды за тем, что кончилось на позиции end (см. _expr_end). Оператор в конце строки (внутри скобок так
    переносит black: «"a" +» ↵ «"v"») или перед «\\» — значение продолжается на следующей строке ("more"); сам
    оператор остаётся виден, вырезаются операнды."""
    while True:
        op = _OPERATOR.match(s, end)
        if op and _REST_EOL.match(s, op.end()):
            return end, "more"
        op = op or (_COMMA_OP.match(s, end) if commas else None)
        if not op or op.end() == len(s):
            return end, None
        if _REST_CONT.match(s, op.end()):
            return end, "more"
        nxt, kind = _operand_end(s, op.end(), commas)
        if kind:
            return nxt, kind
        if nxt == op.end():
            return end, None
        end = nxt


def _value_end(s, i, name, sep, q, to_eol, line_start, commas):
    """Конец значения, начатого на позиции i, и вид продолжения: None, "yaml" (значение — блок ниже), "open"
    (скобка или строка не закрыты на этой строке) или "next" (значение — следующая строка: после «\\», после
    ключа в кавычках без значения). Блок ниже бывает только у имени в начале строки (YAML), а не в коде. В строках
    YAML и INI («password = a b c») простое значение идёт до конца строки. Значение заголовка без кавычек —
    со схемой (Authorization: token X); в коде и JSON ("Authorization": …) — выражение, как у остальных имён."""
    n = len(s)
    if _REST_CONT.match(s, i):
        return n, "next"
    if to_eol and s.startswith(MASK, i) and _REST_END.match(s, i + len(MASK)):
        return i + len(MASK), None  # вырезано прошлым проходом: «Keys: <redacted>,»
    blank = bool(_REST_BLANK.match(s, i))
    if sep == ":" and (blank or _REST_COMMENT.match(s, i) or _BLOCK_SCALAR.match(s, i)):
        if line_start:
            return n, "yaml"
        return (n, "next") if q and blank else (i, None)
    opened = bool(_QUOTE.match(s, i)) or s[i:i + 1] in ("[", "{")
    if to_eol and not opened:
        return _to_eol(s, i, i, sep)
    if _SPACED.search(name) and not q and not opened and not s.startswith(MASK, i):
        return _trim(s, i, _HEADER_VALUE.match(s, i).end(), " \t\\"), None
    end, kind = _expr_end(s, i, commas)
    if to_eol and not kind and not _REST_END.match(s, end):  # token = "a" b: в строке YAML, INI — до конца
        return _to_eol(s, i, end, sep)
    return end, kind


def _to_eol(s, i, j, sep):
    """Значение от позиции i до конца строки или до « #комментария» (он ищется с позиции j, за началом значения,
    вне строк в кавычках: в «key = s.split(" #")» комментария нет) и вид продолжения: "open", если в значении не
    закрыты скобка или строка (key = ''.join(⏎ …); "more", если в коде, INI, shell значение кончается оператором
    (token = x or ⏎ "v"), — он остаётся виден. «\» в конце строки кода, INI, shell — перенос, а не значение:
    продолжение разбирает _redact_line; в YAML «\» — знак значения."""
    end = _trim(s, j, _comment(s, j))
    if sep != ":" and s[end - 1:end] == "\\":
        end = _trim(s, i, end - 1)
    if _opened(s[i:end]):
        return end, "open"
    op = _OP_END.search(s, i, end) if sep != ":" else None
    return (_trim(s, i, op.start()), "more") if op else (end, None)


def _comment(s, j):
    """Позиция « #комментария» за позицией j вне строк в кавычках или конец строки. В YAML « #» внутри кавычек
    посреди простого значения — тоже комментарий, но такое значение прячется вместе с ним: лишнее вырезание
    допустимо, хвост секрета за « #» в коде — нет."""
    if s.find("#", j) >= 0:
        for k, c in _outside(s, j):
            if c == "#" and k > j and s[k - 1] in " \t":
                return k
    return len(s)


def _mask(value, name):
    if not value or _plain(value, name):
        return value
    m = _QUOTE.match(value)
    if m:
        end = _quoted_end(value, m.end(), m.group(1))
        if end is None:
            return value[:m.end()] + MASK
        return value[:m.end()] + MASK + m.group(1) if end == len(value) else MASK
    if value[0] in "[{" and _close(value, 0) == len(value) - 1:  # [a, b] → [<redacted>]; [a] + b — маска целиком
        return value if _ALL_MASKED.match(value[1:-1]) else value[0] + MASK + value[-1]
    return MASK


def _value(line, m, line_start, pair, depth):
    """Значение присваивания, которое начинает совпадение _HEAD (см. комментарий к _HEAD): (начало, конец, вид
    продолжения, запятые — часть значения) или None, если это не присваивание секрету. depth — _Depth строки."""
    q, name, close, sep = m.group("q", "name", "close", "sep")
    if not (secret_name(name) or pair and _PAIR_VALUE_NAME.match(name)):
        return None
    i = m.end()
    if not sep:  # get("NAME") в конце строки: запасное значение — строкой ниже, с «or» (см. _tail)
        if q and close:
            return i, i, None, False
        # имя в кавычках одно на строке — так black переносит его внутри вызова или индекса: значение — за скобкой
        return (i, i, "name", False) if q and not line[:m.start()].strip() else None
    if sep in ("or", "if"):  # запасное значение: get("NAME") or v; у имени без кавычек — только литерал
        if _REST_EOL.match(line, i):  # get("NAME") or ⏎ v: значение — на следующей строке (у имени в кавычках)
            return (i, len(line), "next", False) if q else None
        literal = _QUOTE.match(line, i) if sep == "or" else _ELSE_LITERAL.match(line, i)
        if not (q or close or literal):
            return None
        end, kind = _expr_end(line, i)
        if not kind and end > i and line[end - 1] == ":" and _BLOCK_LINE.match(line):
            end -= 1  # if f(key) or g(key): — «:» заголовка остаётся, по нему его узнаёт и повторный проход
        return i, end, kind, False
    if close == ")":
        return None
    if sep in ("==", "===", "!=", "!=="):  # значение — только литерал справа, строкой ниже не продолжается
        return i, (_operand_end(line, i)[0] if _QUOTE.match(line, i) else i), "end", False
    top = depth.at(m.start("sep")) <= 0  # оператор, а не аргумент вызова или поле объекта
    if sep == ",":
        if q:  # имя в кавычках после «:» или «=» — значение поля, а не ключ
            j = _trim(line, 0, m.start())
            if close or line[j - 1:j] in (":", "="):
                return None
            return (i,) + _args_end(line, i) + (False,)
        t = _TARGETS.match(line, i) if top else None
        return (t.end(),) + _expr_end(line, t.end(), True) + (True,) if t else None
    if sep == ":" and not q and not line_start:
        if top and _BLOCK_LINE.match(line):
            return None
        if _DEF_LINE.match(line):  # def f(token: str = "v"): аннотация остаётся, значение — после «=»
            a = _ANNOTATED.match(line, i)
            if a and _REST_EOL.match(line, a.end()):  # def f(token: str = ⏎ "v"): значение — на следующей строке
                return a.end(), len(line), "next", False
            return (a.end(),) + _expr_end(line, a.end()) + (False,) if a else None
        end, kind = _expr_end(line, i)
        a = None if kind else _ASSIGN.match(line, end)
        if a:  # «token: x = v» вне def: и аннотация, и значение
            return (i,) + _expr_end(line, a.end()) + (False,)
    if sep != ":" and not top and _REST_EOL.match(line, i):
        return i, len(line), "next", False  # f(token=\n "v"): значение — на следующей строке
    commas = top and sep != ":"
    spaced = bool(m.group("sp") or m.group("sp2"))
    to_eol = line_start and not q and (sep == ":" or sep == "=" and spaced)
    return (i,) + _value_end(line, i, name, sep, q, to_eol, line_start, commas) + (commas,)


def _starts(line, pair):
    """Где может начинаться присваивание: от каждой опорной подстроки (в строке пары — и от value, default) влево
    по знакам имени, не дальше 64, как в _SECRET_NAME, и на кавычку перед именем. Длинная строка без разделителей
    (base64 картинки в журнале) так проходится за линейное время."""
    low, hits = line.lower(), []
    for a in _ANCHORS:
        j = low.find(a)
        while j >= 0:
            hits.append(j)
            j = low.find(a, j + 1)
    if pair:
        hits += [w.start() for w in _PAIR_VALUE_RX.finditer(line)]
    starts = set()
    for j in hits:
        j = _name_start(line, j, 64)
        starts.add(j)
        if j and line[j - 1] in "\"'":
            q = j - 1
            while q and line[q - 1] == "\\":
                q -= 1
            starts.add(q)
    return sorted(starts)


def _name_start(line, j, limit):
    """Начало имени, которое кончается (или содержит опорную подстроку) на позиции j: влево по знакам имени,
    не дальше limit."""
    stop = max(0, j - limit)
    while j > stop and line[j - 1] in _NAME_CHARS:
        j -= 1
    return j


def _urlencoded(line):
    """Значения секретных имён в URL-кодировке; поиск идёт от разделителя %3D, %3A."""
    out, pos = [], 0
    for sep in _URL_SEP.finditer(line):
        value = _URL_VALUE.match(line, sep.end())
        name = line[_name_start(line, sep.start(), 160):sep.start()]
        if sep.start() >= pos and value and secret_name(name):
            out += [line[pos:sep.end()], MASK]
            pos = value.end()
    out.append(line[pos:])
    return "".join(out)


def _redact_line(line, pair=False):
    """Одна строка без учёта соседних (узнаваемые значения, присваивания, флаги) и открытый блок: None,
    ("yaml", отступ), ("open", вложенность, кавычка), ("next", запятые, взято ли уже значение — тогда новая пара
    или флаг на следующей строке не продолжение), ("op", запятые, кончилось ли строкой в кавычках), ("args",
    первая ли строка списка ниже) или ("name",) — см. _tail. pair — строка с полем-значением пары, имя которой
    названо выше."""
    pair = pair or any(secret_name(m.group("v")) for m in _PAIR.finditer(line))
    low = line.lower()
    for need, rx, repl in PATTERNS:
        if need in low:
            line = rx.sub(repl, line)
    line = _urlencoded(line)
    out, pos, block, lead, depth = [], 0, None, _LEAD.match(line).end(), _Depth(line)
    for start in _starts(line, pair):
        m = _HEAD.match(line, start) if start >= pos else None
        got = m and _value(line, m, start <= lead, pair, depth)
        if not got:
            continue
        i, end, kind, commas = got
        out.append(line[pos:i])
        if kind == "open":
            text, block = _open(line, i)
            out.append(text)
            pos = len(line)
            break
        out.append(line[i:end] if kind in ("next", "yaml") else _mask(line[i:end], m.group("name")))
        pos = end
        if kind == "yaml":
            block = ("yaml", len(line) - len(line.lstrip(" ")))
            break
        block = _tail(line, end, kind, commas)
        if block and block[0] in ("next", "args", "name"):
            break
    out.append(line[pos:])
    line, flag_block = _flags("".join(out))
    return line, block or flag_block


def _tail(s, end, kind, commas):
    """Блок за значением, которое кончилось на позиции end, по виду продолжения kind (см. _value_end, _expr_tail,
    _args_end; "end" — литерал в сравнении): ("next", …) — значение и на следующей строке (после «\\», оператора в
    конце строки, ключа без значения); ("args", первая ли строка ниже) — остаток списка аргументов на следующих
    строках; ("name",) — имя одно на строке, значение — за скобкой ниже; ("op", запятые, кончилось ли строкой в
    кавычках) — значение дошло до конца строки и продолжается следующей, если та начинается с оператора или, за
    строкой в кавычках, со строки (склейка; внутри скобок так переносит black: «api_key=x» ↵ «or "v"»,
    «api_key="a"» ↵ «"b"», см. Redactor.feed); None — значение кончилось."""
    if kind == "next":
        return "next", commas, False
    if kind in ("args", "kwargs"):
        return "args", kind == "args"
    if kind == "name":
        return ("name",)
    if kind == "more" or _REST_CONT.match(s, end):
        return "next", commas, True
    if kind != "end" and _REST_EOL.match(s, end):
        return "op", commas, s[end - 1:end] in ("\"", "'")
    return None


def _after(s, p):
    """Остаток строки за значением, которое закрылось на позиции p, и открытый блок. Продолжение выражения —
    вызовы, индексы, операнды через +, or … (`).strip() or "v"`) — тоже значение. За «]» и «=» значение — после «=»:
    так black переносит аннотацию типа внутри скобок (`api_key: Optional[` ↵ `str` ↵ `] = v`). Дальше — обычная
    строка."""
    a = _ASSIGN.match(s, p) if s[p - 1] == "]" else None
    if a:
        start = a.end()
        if _REST_EOL.match(s, start):  # «] =» в конце строки: значение — на следующей
            rest, block = _redact_line(s[p:])
            return rest, block or _tail(s, start, "next", False)
        end, kind = _expr_end(s, start)
    else:
        t, kind = _operand_end(s, p) if s[p:p + 1] in (".", "[", "(") else (p, None)
        end, kind = (t, kind) if kind else _expr_tail(s, t)
        if end == p:
            rest, block = _redact_line(s[p:])
            return rest, block or _tail(s, p, kind, False)
        start = p if t > p else _OPERATOR.match(s, p).end()  # «) or "v"»: оператор остаётся виден
    if kind == "open":
        text, block = _open(s, start)
        return s[p:start] + text, block
    rest, block = _redact_line(s[end:])
    return s[p:start] + _mask(s[start:end], "") + rest, block or _tail(s, end, kind, False)


def _flags(line):
    """Значения флагов-секретов (--api-key VALUE) и открытый блок ("next", если значение — следующая строка)."""
    out, pos = [], 0
    for f in _FLAG.finditer(line):
        if f.start() < pos or not secret_name(f.group(1)):
            continue
        if _REST_CONT.match(line, f.end()):
            out.append(line[pos:])
            return "".join(out), ("next", True, False)
        end, _ = _operand_end(line, f.end(), True)
        out += [line[pos:f.end()], _mask(line[f.end():end], f.group(1))]
        pos = end
    out.append(line[pos:])
    return "".join(out), None


class Redactor:
    """Построчное вырезание с памятью о многострочных значениях: блок YAML под секретным ключом, незакрытые скобка
    или строка (в том числе в тройных кавычках), продолжение на следующей строке, остаток списка аргументов за
    именем в кавычках, блок PRIVATE KEY, пара «- name: NAME» с полем value ниже."""

    def __init__(self):
        self.block = None
        self.pem = False
        self.pair = None  # отступ полей пары «имя — значение», названной строкой выше

    def feed(self, line):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        if self.pem:
            if _PEM_END.search(body):
                self.pem = False
                return line
            return MASK + eol
        stripped = body.lstrip(" \t")
        indent = len(body) - len(stripped)
        kind = self.block[0] if self.block else None
        chain = None
        if kind == "op":
            # значение дошло до конца строки выше; строка с оператора продолжает его, если выражение за оператором
            # кончается разделителем или скобкой («    or "v",», «    + "v")»), а не сравнением или текстом
            # («    or token == "v"):», «not None and the …»). За строкой в кавычках так же продолжает строка в
            # кавычках — склейка («    "v",»). «if c» — условие тернарного выражения: значение — в ветке else ниже
            # (так переносит black), если её нет на этой же строке. Пустая строка и комментарий цепочку не рвут
            chain, self.block, kind = self.block, None, None
            commas = chain[1]
            if stripped and not stripped.startswith("#"):
                op = _LINE_OP.match(stripped)
                glued = chain[2] and _QUOTE.match(stripped)
                chain = None
                if glued or op and not _NEW_ITEM.match(stripped, op.end()):
                    start = indent if glued else _LEAD_OP.match(body, indent).end()
                    end, more = _expr_end(body, start, commas)
                    if more or _REST_VALUE.match(body, end):
                        if glued or op.group(1) != "if" or _ELSE.search(body, start, end):
                            return self._continue(body, start, end, more, commas) + eol
                        chain = ("op", commas, False)
        if kind == "next":
            if not stripped or stripped == "\\":
                return line
            if not (self.block[2] and _NEW_ITEM.match(stripped)):
                # за уже взятым значением строка продолжает выражение и бывает с оператора: «    or "v"»
                start = _LEAD_OP.match(body, indent).end() if self.block[2] else indent
                end, more = _expr_end(body, start, self.block[1])
                return self._continue(body, start, end, more, self.block[1]) + eol
            self.block = None
        if kind == "args":
            # остаток списка аргументов за именем в кавычках, как его переносит black, по аргументу на строке
            # («    "--token",» ↵ «    default="v",»): первая строка — значение, дальше — значения именованных
            # аргументов до строки другого вида (в списке строк прячется только элемент за именем); пустая строка и
            # комментарий список не прерывают
            if not stripped or stripped.startswith("#"):
                return _redact_line(body)[0] + eol
            kw = _KWARG.match(body, indent)
            if kw or self.block[1]:
                start = kw.end() if kw else indent
                end, more = _expr_end(body, start)
                text = self._continue(body, start, end, more, False)
                if not more and _ARG_COMMA.match(body, end):
                    self.block = ("args", False)
                return text + eol
            self.block = None
        if kind == "name":
            # имя секрета одно на строке внутри вызова или индекса («    "VK_TOKEN"»): значение — за закрывающей
            # скобкой в начале этой строки («] = v», «) or v»; «)» в конце строки — «or v» строкой ниже)
            self.block, kind = None, None
            if stripped[:1] in (")", "]"):
                rest, self.block = _after(body, indent + 1)
                return body[:indent + 1] + rest + eol
        if kind == "open":
            text, end, depth, quote = _walk(body, indent, self.block[1], self.block[2], True)
            if end is None:
                self.block = _open_block(depth, quote)
                return body[:indent] + text + eol
            rest, self.block = _after(body, end)
            return body[:indent] + text + rest + eol
        if kind == "yaml":
            item = stripped == "-" or stripped.startswith("- ")
            if not stripped:
                return line
            if indent > self.block[1] or (indent == self.block[1] and item):
                return body[:indent] + ("- " if item else "") + MASK + eol
            self.block = None
        if _PEM_BEGIN.search(body) and not _PEM_END.search(body):
            self.pem = True
        if self.pair is not None and stripped and indent < self.pair:
            self.pair = None
        sibling = self.pair is not None and indent == self.pair
        item = _PAIR_ITEM.match(body)
        body, self.block = _redact_line(body, sibling)
        self.block = self.block or chain
        if item and secret_name(item.group("v")):
            self.pair = len(item.group("ind"))
        return body + eol

    def _continue(self, body, start, end, kind, commas):
        """Строка, которая продолжает значение: выражение body[start:end] (kind — вид продолжения у _expr_end) —
        маска, остаток — обычная строка."""
        if kind == "open":
            text, self.block = _open(body, start)
            return body[:start] + text
        rest, block = _redact_line(body[end:])
        self.block = block or _tail(body, end, kind, commas)
        return body[:start] + _mask(body[start:end], "") + rest


def _hide(name):
    """Что прятать под ключом JSON: None — ничего, "str" — строки (счётчик токенов), "all" — строки и числа."""
    if not secret_name(name):
        return None
    return "str" if _counter(name) else "all"


def scrub(obj, hide=None):
    """JSON-объект без секретов: под секретным ключом строки (и числа, если ключ не счётчик) — MASK; в объекте,
    где name/key/id называет секрет, — и value/default; в списке — элемент после флага-секрета или секретного
    имени («--token», "VK_TOKEN"); остальные строки — через redact_lines."""
    if isinstance(obj, dict):
        pair = any(str(k).lower() in _PAIR_KEYS and isinstance(v, str) and secret_name(v) for k, v in obj.items())
        return {k: scrub(v, hide or _hide(str(k)) or ("all" if pair and _PAIR_VALUE_NAME.match(str(k)) else None))
                for k, v in obj.items()}
    if isinstance(obj, list):
        out, hide_next = [], False
        for v in obj:
            out.append(MASK if hide_next and isinstance(v, str) and v else scrub(v, hide))
            hide_next = isinstance(v, str) and secret_name(v)
        return out
    if isinstance(obj, str):
        return MASK if hide and obj else redact_lines(obj)
    if hide == "all" and isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return MASK
    return obj


def redact_lines(text):
    r = Redactor()
    return "".join(r.feed(line) for line in text.splitlines(keepends=True))


def redact_json(text):
    try:
        obj = json.loads(text)
    except ValueError:
        return redact_lines(text)
    return json.dumps(scrub(obj), ensure_ascii=False, indent=2) + "\n"


def redact_file_text(path, text):
    return redact_json(text) if path.endswith(".json") else redact_lines(text)


def _decode(data):
    return data.decode("utf-8", "surrogateescape")  # байты не в UTF-8 проходят без потерь


def _encode(text):
    return text.encode("utf-8", "surrogateescape")


def check(root):
    """Повторный проход по каталогу; исправленные файлы — в stdout, по одному на строку."""
    for base, _, files in os.walk(root):
        for name in sorted(files):
            path = os.path.join(base, name)
            with open(path, "rb") as f:
                text = _decode(f.read())
            new = redact_file_text(path, text)
            if new != text:
                with open(path, "wb") as f:
                    f.write(_encode(new))
                print(os.path.relpath(path, root))


def main(argv):
    if len(argv) == 2 and argv[0] == "--check":
        check(argv[1])
    elif argv == ["--json"]:
        sys.stdout.buffer.write(_encode(redact_json(_decode(sys.stdin.buffer.read()))))
    elif not argv:
        r = Redactor()
        for raw in sys.stdin.buffer:
            sys.stdout.buffer.write(_encode(r.feed(_decode(raw))))
    else:
        sys.stderr.write(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
