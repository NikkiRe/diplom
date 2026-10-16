"""redact.py: что вырезается (ключи узнаваемого вида; значение, присвоенное секретному имени, в формах
присваивания — JSON, YAML, env, юниты, аргументы, код, XML, JSON внутри строки JSON, пары «имя — значение»,
несколько целей, аннотации типа, списки через запятую, склейка строк, лямбда, « #» в кавычках, значение на следующих
строках — и переносы black внутри скобок: у оператора, склейка, список аргументов по строке, имя одно на строке,
аннотация; незакрытые скобки и строки; числа тоже), что остаётся читаемым (пределы и счётчики токенов, пустые
значения, текст ошибок, код вокруг, строка с оператора, которая значение не продолжает), повторный проход ничего не
меняет (проверяется у каждого образца), длинная строка обрабатывается за линейное время, режим --check исправляет
файлы на месте. Только стандартная библиотека — запускается и под Python 3.8."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
sys.dont_write_bytecode = True  # у redact.py (его копируют лаборатории) не появится __pycache__ и без -B

import redact  # noqa: E402

M = redact.MASK


def lines(text):
    return redact.redact_lines(text)


def check(test, cases, eol="\n"):
    """Каждый образец (к однострочным добавляется eol): вывод как ожидается, повторный проход ничего не меняет —
    иначе повторная проверка в collect_diag.sh дала бы ложную «ошибку»."""
    for src, want in cases.items():
        with test.subTest(src=src):
            got = lines(src + eol)
            test.assertEqual(got, want + eol)
            test.assertEqual(lines(got), got)


class PatternsTest(unittest.TestCase):
    def test_known_key_shapes_anywhere(self):
        cases = {
            "error: invalid key sk-TEST1234567890abcdef for model": f"error: invalid key {M} for model",
            "sk-proj-TESTabcdefgh12345678 and sk-ant-TEST_abcdefgh": f"{M} and {M}",
            "remote: https://oauth2:glpat-TESTabcdefghijklmnopqr@gitlab.se.ifmo.ru/x.git":
                f"remote: https://{M}@gitlab.se.ifmo.ru/x.git",
            "token ghp_TESTabcdefghijklmnopqrstuvwxyz0123 used": f"token {M} used",
            "jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Q": f"jwt {M}",
            "bot 123456789:AAEhBP0av28Y3GjKFzZp8cQ7nT1xWvYq0aB ok": f"bot {M} ok",
            'Post "https://api.telegram.org/bot123456789:TESTtgURLtoken0123456789abcdefghijk/getUpdates"':
                f'Post "https://api.telegram.org/bot{M}/getUpdates"',
            "vk Long poll failed: https://lp.vk.com/whp/227?act=a_check&key=" + "0123456789abcdef" * 2 + "TEST0001"
            "&ts=42&wait=25": f"vk Long poll failed: https://lp.vk.com/whp/227?act=a_check&key={M}&ts=42&wait=25",
            "GET /api?sig=TESTsig0123&v=5": f"GET /api?sig={M}&v=5",
            "aws " + "AKIA" + "TESTABCDEFGHIJKL": f"aws {M}",
            "mqtt://robot:TESTpass@127.0.0.1:1883": f"mqtt://{M}@127.0.0.1:1883",
            "curl -H 'X: Bearer TESTbearer0123'": f"curl -H 'X: Bearer {M}'",
            "Authorization Basic dXNlcjpwYXNzd29yZA==": f"Authorization Basic {M}",
            "python3 bot.py vk1.a.TESTvkShape0123456789abcdef": f"python3 bot.py {M}",
            "vk group " + "0123456789abcdef" * 5 + "01234": f"vk group {M}",
            "git clone https://0123456789abcdefTESTtoken0@github.com/x.git": f"git clone https://{M}@github.com/x.git",
            "curl -u robot:TESTcurlPass http://127.0.0.1/": f"curl -u robot:{M} http://127.0.0.1/",
            "curl --user=robot:TESTcurl2 http://x": f"curl --user=robot:{M} http://x",
            "echo TESTsudo | sudo -S jetson_clocks": f"echo {M} | sudo -S jetson_clocks",
            "alias pm=\"printf '%s\\n' TESTsudo2 | sudo -kS nvpmodel -m 0\"":
                f"alias pm=\"printf {M} | sudo -kS nvpmodel -m 0\"",
            "sudo -S <<< \"TESTsudo3\" jetson_clocks": f"sudo -S <<< {M} jetson_clocks",
            "echo \"jetson:TESTsudo4\" | sudo chpasswd": f"echo {M} | sudo chpasswd",
            "sshpass -p 'TESTsshpass' ssh jetson@robot": f"sshpass -p {M} ssh jetson@robot",
            "GET /method/messages.send?access_token=TESTq1&v=5.199 200":
                f"GET /method/messages.send?access_token={M}&v=5.199 200",
            "GET /x?q=%22api_key%22%3A%22TESTenc1%22%2C%22model%22%3A%22gpt%22":
                f"GET /x?q=%22api_key%22%3A%22{M}%22%2C%22model%22%3A%22gpt%22",
            "body=api_key%3DTESTenc2%26model%3Dgpt": f"body=api_key%3D{M}%26model%3Dgpt",
            'nmcli dev wifi hotspot ifname wlan0 ssid NeuroBot password "TESTnm1 x"':
                f"nmcli dev wifi hotspot ifname wlan0 ssid NeuroBot password {M}",
            "nmcli con modify lab wifi-sec.psk 'TESTnm2' wifi-sec.key-mgmt wpa-psk":
                f"nmcli con modify lab wifi-sec.psk {M} wifi-sec.key-mgmt wpa-psk",
            "wpa_passphrase 'Lab Net' TESTwp1 > /etc/wpa.conf": f"wpa_passphrase 'Lab Net' {M} > /etc/wpa.conf",
        }
        check(self, cases)

    def test_plain_words_survive(self):
        for text in ("Using basic settings", "Bearer token missing", "task-runner started", "risk-free",
                     "https://github.com/sipeed/picoclaw", "RuntimeError: Frame didn't arrive within 5000",
                     "robot12345678:short", "use_keyboard: true", "default_keymap: us", "author: Robot",
                     "journalctl -u nb-lab.service --since -1d", "echo hello | grep h", "ssh://git@github.com/x.git",
                     "KeyError: 'VK_TOKEN'", 'raise RuntimeError("VK_TOKEN не задан")',
                     'logger.info("loaded token for %s", user)', '{"level":"info","msg":"token","count":3}',
                     "KeyboardInterrupt", "PassEnvironment=VK_TOKEN MODE", "monkey: 1", "bypass: true",
                     "for key in keys:", "keyring==23.5.0", "secretstorage==3.3.1", "token_count == 0",
                     "if len(token) == 0:", "if password == expected:", "OSError: [Errno 98] Address already in use",
                     'here=$(cd "$(dirname "$0")" && pwd)', "nmcli -s con show Lab | grep psk"):
            with self.subTest(text=text):
                self.assertEqual(lines(text + "\n"), text + "\n")


class PairsTest(unittest.TestCase):
    def test_env_shell_and_units(self):
        self.assertEqual(lines("export OPENAI_API_KEY=abc123\n"), f"export OPENAI_API_KEY={M}\n")
        self.assertEqual(lines('ROUTERAI_TOKEN="a b c"\n'), f'ROUTERAI_TOKEN="{M}"\n')
        self.assertEqual(lines('Environment="ROUTERAI_TOKEN=abc" "MODE=1"\n'),
                         f'Environment="ROUTERAI_TOKEN={M}" "MODE=1"\n')
        self.assertEqual(lines("Environment=API_KEY=abc MODE=1\n"), f"Environment=API_KEY={M} MODE=1\n")

    def test_names_ending_in_key(self):
        cases = {
            "export ROUTERAI_KEY=TESTabc": f"export ROUTERAI_KEY={M}",
            'export CLOUDRU_KEY="TEST q"': f'export CLOUDRU_KEY="{M}"',
            "Environment=VK_GROUP_KEY=TESTvk MODE=1": f"Environment=VK_GROUP_KEY={M} MODE=1",
            "VK_GROUP_KEY_ID=TESTid": f"VK_GROUP_KEY_ID={M}",
            "python3 worker.py --key TESTk --port 8766": f"python3 worker.py --key {M} --port 8766",
        }
        check(self, cases)

    def test_short_words_and_camel_case(self):
        cases = {
            '"key": "TESTw1",': f'"key": "{M}",',
            "export SUDO_PASS=TESTw2": f"export SUDO_PASS={M}",
            "export SSHPASS=TESTw3": f"export SSHPASS={M}",
            'psk="TESTw4"': f'psk="{M}"',
            "MYSQL_PWD=TESTw5": f"MYSQL_PWD={M}",
            "authKey=TESTw6 groupKey: TESTw7": f"authKey={M} groupKey: {M}",
            "--auth TESTw8": f"--auth {M}",
            "pwd = 'TESTw9'": f"pwd = '{M}'",
            '"psw": "TESTw10", db_pswd=TESTw11': f'"psw": "{M}", db_pswd={M}',
        }
        check(self, cases)

    def test_json_line_and_numbers(self):
        src = '{"level":"error","api_key":"x1","max_tokens":32768,"token":"","msg":"connection closed"}\n'
        want = f'{{"level":"error","api_key":"{M}","max_tokens":32768,"token":"","msg":"connection closed"}}\n'
        self.assertEqual(lines(src), want)

    def test_yaml_scalars_lists_blocks(self):
        src = ("model_list:\n"
               "  gpt:\n"
               "    api_keys:\n"
               '      - "keyone"\n'
               "      - keytwo\n"
               "    api_base: https://routerai.ru/api/v1\n"
               "channels:\n"
               "  telegram:\n"
               "    token: 123:abc def  # бот\n"
               "  mqtt:\n"
               "    password: |\n"
               "      line1\n"
               "    username: robot\n"
               "max_tokens: 1024\n")
        want = ("model_list:\n"
                "  gpt:\n"
                "    api_keys:\n"
                f"      - {M}\n"
                f"      - {M}\n"
                "    api_base: https://routerai.ru/api/v1\n"
                "channels:\n"
                "  telegram:\n"
                f"    token: {M}  # бот\n"
                "  mqtt:\n"
                "    password: |\n"
                f"      {M}\n"
                "    username: robot\n"
                "max_tokens: 1024\n")
        self.assertEqual(lines(src), want)

    def test_yaml_compact_list_and_flow(self):
        self.assertEqual(lines('api_keys:\n- "a"\nnext: 1\n'), f"api_keys:\n- {M}\nnext: 1\n")
        self.assertEqual(lines('api_keys: ["a", "b"]\n'), f"api_keys: [{M}]\n")
        self.assertEqual(lines("api_keys: []\n"), "api_keys: []\n")

    def test_json_multiline_array(self):
        src = '  "api_keys": [\n    "a",\n    "b"\n  ],\n  "model": "gpt"\n'
        want = f'  "api_keys": [\n    "{M}",\n    "{M}"\n  ],\n  "model": "gpt"\n'
        self.assertEqual(lines(src), want)

    def test_flags(self):
        self.assertEqual(lines("python3 s.py --api-key qwerty --max-tokens 10 --config c.json\n"),
                         f"python3 s.py --api-key {M} --max-tokens 10 --config c.json\n")
        self.assertEqual(lines("picoclaw --token=abc gateway\n"), f"picoclaw --token={M} gateway\n")

    def test_limits_are_not_secrets_other_picoclaw_fields_are(self):
        text = "max_tokens_field: max_completion_tokens\n"
        self.assertEqual(lines(text), text)
        for name in ("encrypt_key", "app_secret", "bot_token", "sasl_password", "crypto_passphrase", "apiKey"):
            with self.subTest(name=name):
                self.assertEqual(lines(f'"{name}": "v"\n'), f'"{name}": "{M}"\n')

    def test_numbers_under_secret_names_counters_stay(self):
        cases = {
            "MQTT_PASSWORD=31337424": f"MQTT_PASSWORD={M}",
            "    password: 20261014": f"    password: {M}",
            "python3 server.py --password 987654": f"python3 server.py --password {M}",
            '"pin_code": 1, "password": 4242,': f'"pin_code": 1, "password": {M},',
            "summarize_token_percent: 75": "summarize_token_percent: 75",
            '"default_token_budget": 0': '"default_token_budget": 0',
            "python3 s.py --max-tokens 10": "python3 s.py --max-tokens 10",
        }
        check(self, cases)

    def test_comparison_is_not_assignment_but_literal_is_secret(self):
        self.assertEqual(lines("if password == x:\n"), "if password == x:\n")
        self.assertEqual(lines('if password == "TESTcmp1":\n'), f'if password == "{M}":\n')
        self.assertEqual(lines("if token != 'TESTcmp2': return\n"), f"if token != '{M}': return\n")

    def test_private_key_block(self):
        src = "a\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----\nb\n"
        want = f"a\n-----BEGIN OPENSSH PRIVATE KEY-----\n{M}\n{M}\n-----END OPENSSH PRIVATE KEY-----\nb\n"
        self.assertEqual(lines(src), want)


class CodeFormsTest(unittest.TestCase):
    """Код и журналы, которые копируются целиком: скрипты воркеров и запуска, изменённый код, консоли tmux."""

    def test_assignment_by_key_attribute_and_operators(self):
        check(self, {
            'os.environ["VK_TOKEN"] = "TESTsub1"': f'os.environ["VK_TOKEN"] = "{M}"',
            "config['token'] = 'TESTsub2'": f"config['token'] = '{M}'",
            "env[API_KEY]=TESTsub3": f"env[API_KEY]={M}",
            'config.token = "TESTsub4"': f'config.token = "{M}"',
            "TOKEN ?= TESTsub5": f"TOKEN ?= {M}",
            'TOKEN += "TESTsub6"': f'TOKEN += "{M}"',
            '"VK_TOKEN" => "TESTsub7"': f'"VK_TOKEN" => "{M}"',
        })

    def test_arguments_after_quoted_name(self):
        check(self, {
            'os.environ.setdefault("ROUTERAI_API_KEY", "TESTarg1")':
                f'os.environ.setdefault("ROUTERAI_API_KEY", "{M}")',
            'os.getenv("VK_TOKEN", default="TESTarg2")': f'os.getenv("VK_TOKEN", {M})',
            'parser.add_argument("--token", type=str, default="TESTarg3")':
                f'parser.add_argument("--token", {M})',
            'subprocess.run(["picoclaw", "--token", tok])': f'subprocess.run(["picoclaw", "--token", {M}])',
            'getattr(cfg, "api_key", "TESTarg4")': f'getattr(cfg, "api_key", "{M}")',
        })

    def test_fallback_after_quoted_name(self):
        check(self, {
            'bot = Bot(os.environ.get("VK_TOKEN") or "TESTfb1")': f'bot = Bot(os.environ.get("VK_TOKEN") or "{M}")',
            'x = os.environ["VK_TOKEN"] if "VK_TOKEN" in os.environ else "TESTfb2"':
                f'x = os.environ["VK_TOKEN"] if {M}',
        })

    def test_whole_expression_on_the_right(self):
        check(self, {
            'token = os.getenv("VK_TOKEN", "TESTexpr1")': f"token = {M}",
            'TOKEN = os.environ.get("VK_TOKEN") or "TESTexpr2"': f"TOKEN = {M}",
            'headers["Authorization"] = "token " + TOKEN': f'headers["Authorization"] = {M}',
            'api_key = f"{prefix}TESTexpr3"': f'api_key = f"{M}"',
            "token = ''.join([\"TESTexpr4\"])": f"token = {M}",
            'TOKEN = """TESTexpr5"""': f'TOKEN = """{M}"""',
            'client(api_key=cfg.get("k") or "TESTexpr6", timeout=5)': f"client(api_key={M}, timeout=5)",
            "password = a b TESTini  # c": f"password = {M}  # c",
            "TOKEN=TESTsh python3 run.py": f"TOKEN={M} python3 run.py",
            "password: 'it''s TESTyaml'": f"password: '{M}'",
            "TOKEN=\"TESTcat1\"'TESTcat2'": f"TOKEN={M}",
            "TOKEN=$'TESTcat3'": f"TOKEN=$'{M}'",
            'python3 w.py --api-key "TESTflag a" --port 1': f'python3 w.py --api-key "{M}" --port 1',
        })

    def test_annotations_conditions_targets_and_lists(self):
        check(self, {
            'def f(token: str = "TESTan1"):': f'def f(token: str = "{M}"):',
            'async def main(token: str = "TESTan2", retries: int = 3):':
                f'async def main(token: str = "{M}", retries: int = 3):',
            "def f(self, key: Optional[str] = KEY):": f"def f(self, key: Optional[str] = {M}):",
            '    token: str = "TESTan3"': f"    token: {M}",
            'if not token: token = "TESTan4"': f'if not token: token = "{M}"',
            "else: password = 'TESTan5'": f"else: password = '{M}'",
            'x = token or "TESTan6"': f'x = token or "{M}"',
            'x = token if token else "TESTan7"': f"x = token if {M}",
            'token = "TESTan8" if not cfg.token else cfg.token': f"token = {M}",
            'token = "%s" % "TESTan9"': f"token = {M}",
            'TOKEN, CHAT_ID = "TESTtu1", 123': f"TOKEN, CHAT_ID = {M}",
            'self.token, self.chat = "TESTtu2", 1': f"self.token, self.chat = {M}",
            'CHAT_ID, TOKEN = 123, "TESTtu3"': f"CHAT_ID, TOKEN = {M}",
            'TOKEN = "TESTtu4", "TESTtu5"': f"TOKEN = {M}",
            "export API_KEYS=TESTli1,TESTli2": f"export API_KEYS={M}",
            "API_KEYS=TESTli3,TESTli4 python3 x.py": f"API_KEYS={M} python3 x.py",
            "python3 w.py --api-keys TESTli5,TESTli6 --port 1": f"python3 w.py --api-keys {M} --port 1",
            '    api_key="TESTli7", timeout=5,': f'    api_key="{M}", timeout=5,',
        })

    def test_struct_fields_and_declarations(self):
        """Поле структуры Go и объявление (const, local) — как присваивание в начале строки; запятая за вырезанным
        значением при повторном проходе остаётся."""
        check(self, {
            '\t\tBraveAPIKeys:    []string{"TESTst1"},': f"\t\tBraveAPIKeys:    {M},",
            '\t\tSessionKey: "agent:" + channel + ":TESTst2",': f"\t\tSessionKey: {M},",
            "\t\tConfiguredSecrets: []string{},": f"\t\tConfiguredSecrets: {M},",
            'const token = process.env.TOKEN || "TESTst3";': f"const token = {M}",
            'local token="TESTst4"': f'local token="{M}"',
        })

    def test_open_brackets_and_strings_continue_below(self):
        """Скобка или строка не закрыта на строке: значение — до закрытия, в любых кавычках и без них, и когда
        скобка открывается за первым операндом (''.join(, "a" % (). От него остаются скобки, знаки препинания,
        операторы и литералы; ключи словаря и комментарии тоже прячутся."""
        cases = {
            'TOKEN = (\n    "TESTml1"\n    "TESTml2"\n)\nafter = "x"\n':
                f'TOKEN = (\n    "{M}"\n    "{M}"\n)\nafter = "x"\n',
            'c = Client(\n    api_key=os.getenv(\n        "K", "TESTml3"),\n    timeout=5,\n)\n':
                f'c = Client(\n    api_key={M}(\n        "{M}", "{M}"),\n    timeout=5,\n)\n',
            "credentials = {\n    'user': 'robot',\n    'password': 'TESTml4',\n}\nafter = 1\n":
                f"credentials = {{\n    '{M}': '{M}',\n    '{M}': '{M}',\n}}\nafter = 1\n",
            "TOKENS = [\n    'TESTml5',  # основной\n    # 'TESTml6',\n]\n":
                f"TOKENS = [\n    '{M}',  # {M}\n    # {M}\n]\n",
            "API_KEYS = (\n    'TESTml7'\n)\n": f"API_KEYS = (\n    '{M}'\n)\n",
            "token = os.getenv(\n    'VK_TOKEN',\n    'TESTml8',\n)\n": f"token = {M}(\n    '{M}',\n    '{M}',\n)\n",
            'api_keys: [\n  TESTml9,\n  "TESTml10", true\n]\nmodel: gpt\n':
                f'api_keys: [\n  {M},\n  "{M}", true\n]\nmodel: gpt\n',
            'TOKEN = """\nTESTml11\n"""\nx = 1\n': f'TOKEN = """\n{M}\n"""\nx = 1\n',
            "TOKEN = '''TESTml12\nTESTml13'''\nx = 1\n": f"TOKEN = '''{M}\n{M}'''\nx = 1\n",
            'TOKEN = "TESTml14\\\nTESTml15"\nuser = 1\n': f'TOKEN = "{M}\n{M}"\nuser = 1\n',
            'API_KEY = os.getenv(\n    "ROUTERAI_API_KEY"\n) or "TESTml16"\nafter = 1\n':
                f'API_KEY = {M}(\n    "{M}"\n) or "{M}"\nafter = 1\n',
            "c = Client(api_key=get(\n    'TESTml17'), timeout=5)\n":
                f"c = Client(api_key={M}(\n    '{M}'), timeout=5)\n",
            "secrets = {\n    \"vk\": {\n        \"token\": 'TESTml18',\n    },\n}\nafter = 1\n":
                f"secrets = {{\n    \"{M}\": {{\n        \"{M}\": '{M}',\n    }},\n}}\nafter = 1\n",
            'API_KEY = os.getenv("K", """TESTml19\nTESTml20""")\nx = 1\n':
                f'API_KEY = {M}("{M}", """{M}\n{M}""")\nx = 1\n',
            "key = ''.join([\n    'TESTml21',\n])\nx = 1\n": f"key = ''{M}([\n    '{M}',\n])\nx = 1\n",
            "user_pass = '%s:%s' % (unquote(user),\n                       unquote(TESTml22))\nx = 1\n":
                f"user_pass = '{M}' % ({M}({M}),\n                       {M}({M}))\nx = 1\n",
            'password = "a" + b - (\n    "TESTml23"\n)\nx = 1\n': f'password = "{M}" + {M} - (\n    "{M}"\n)\nx = 1\n',
        }
        check(self, cases, eol="")

    def test_unclosed_plain_quote_ends_with_line(self):
        """Незакрытая обычная кавычка без «\\» в конце — вложенные кавычки (строки с примерами в коде тестов,
        журналы), а не строка на несколько строк: следующая строка разбирается сама по себе."""
        cases = {
            "\"token = ''.join([\\\"x\\\"])\": 1,\n'TOKEN = \"\"\"TESTuq1\"\"\"': 2,\n":
                f"\"token = ''{M}([\\\"{M}\\\"])\"{M}\n'TOKEN = \"\"\"{M}\"\"\"': 2,\n",
            "x = 'TOKEN=\"a' + b\npassword = 'TESTuq2'\n": f"x = 'TOKEN=\"{M}\npassword = '{M}'\n",
        }
        check(self, cases, eol="")

    def test_value_on_the_next_line(self):
        cases = {
            'API_KEY = \\\n    "TESTnx1"\nafter = 1\n': f'API_KEY = \\\n    "{M}"\nafter = 1\n',
            "export VK_TOKEN=\\\n  TESTnx2\n": f"export VK_TOKEN=\\\n  {M}\n",
            "python3 bot.py --token \\\n    TESTnx3 \\\n    --port 8766\n":
                f"python3 bot.py --token \\\n    {M} \\\n    --port 8766\n",
            'os.environ.setdefault(\n    "VK_TOKEN",\n    "TESTnx4",\n)\n':
                f'os.environ.setdefault(\n    "VK_TOKEN",\n    "{M}",\n)\n',
            '{"token":\n "TESTnx5"}\n': f'{{"token":\n "{M}"}}\n',
            'TOKEN = \\\n    "TESTnx6" \\\n    "TESTnx7"\nx = 1\n': f'TOKEN = \\\n    "{M}" \\\n    "{M}"\nx = 1\n',
            'TOKEN = "TESTnx8" \\\n    "TESTnx9"\nx = 1\n': f'TOKEN = "{M}" \\\n    "{M}"\nx = 1\n',
            "send(token=\n     'TESTnx10')\n": f"send(token=\n     '{M}')\n",
            "ENV TOKEN=TESTnx11 \\\n    MODE=1\n": f"ENV TOKEN={M} \\\n    MODE=1\n",
            "docker run -e API_KEY=TESTnx12 \\\n  -e MODE=1 img\n": f"docker run -e API_KEY={M} \\\n  -e MODE=1 img\n",
            "export API_KEY=\nMODE=1\n": "export API_KEY=\nMODE=1\n",
            'TOKEN = os.getenv("T") \\\n    or "TESTnx13"\nx = 1\n': f'TOKEN = {M} \\\n    or "{M}"\nx = 1\n',
            'token = "a" + \\\n    "TESTnx14"\nx = 1\n': f'token = "{M}" + \\\n    "{M}"\nx = 1\n',
            'token = "a" \\\n    + "TESTnx15"\nx = 1\n': f'token = "{M}" \\\n    + "{M}"\nx = 1\n',
            'f(token="a" + \\\n  "TESTnx16")\nx = 1\n': f'f(token="{M}" + \\\n  "{M}")\nx = 1\n',
            "password: C:\\dir\\\nnext: 1\n": f"password: {M}\nnext: 1\n",  # в YAML «\» — знак значения
        }
        check(self, cases, eol="")

    def test_operator_at_line_break_inside_brackets(self):
        """Перенос у оператора без «\\» — как black внутри скобок вызова и словаря: оператор в начале следующей
        строки (and, or, +, %, if ↵ else) или в конце строки; значение — и имя в кавычках перед «or» строкой ниже.
        Оператор остаётся виден, остаток строки разбирается как обычная строка."""
        cases = {
            'c = make_client(\n    base_url=URL,\n    api_key=os.getenv("ROUTERAI_API_KEY")\n    or "TESTop1",\n)\n':
                f'c = make_client(\n    base_url=URL,\n    api_key={M}\n    or "{M}",\n)\n',
            'headers = {\n    "X-Api-Key": os.environ.get("K")\n    or "TESTop2",\n}\n':
                f'headers = {{\n    "X-Api-Key": {M}\n    or "{M}",\n}}\n',
            'send(token="robot-"\n     + "TESTop3")\n': f'send(token="{M}"\n     + "{M}")\n',
            'send(token="robot-" +\n     "TESTop4")\n': f'send(token="{M}" +\n     "{M}")\n',
            'c = Client(api_key=os.getenv("K") or\n    "TESTop5")\n': f'c = Client(api_key={M} or\n    "{M}")\n',
            'h = {"Authorization": "Bearer %s"\n     % ("TESTop6",)}\n':
                f'h = {{"Authorization": "{M}"\n     % {M}}}\n',
            'c = Client(\n    api_key=os.getenv("K")\n    if prod\n    else "TESTop7",\n)\n':
                f'c = Client(\n    api_key={M}\n    if prod\n    else "{M}",\n)\n',
            'c = Client(\n    api_key=os.getenv("K")\n    if prod else "TESTop8",\n)\n':
                f'c = Client(\n    api_key={M}\n    if {M},\n)\n',
            'bot = Bot(\n    os.environ.get("VK_TOKEN")\n    or "TESTop9"\n)\n':
                f'bot = Bot(\n    os.environ.get("VK_TOKEN")\n    or "{M}"\n)\n',
            'bot = Bot(os.environ.get("VK_TOKEN") or\n    "TESTop10")\n':
                f'bot = Bot(os.environ.get("VK_TOKEN") or\n    "{M}")\n',
            'c = Client(\n    token = os.getenv("T") or\n    "TESTop11")\n':
                f'c = Client(\n    token = {M} or\n    "{M}")\n',
            'def f(token: str =\n        "TESTop12"): pass\n': f'def f(token: str =\n        "{M}"): pass\n',
            'f(\n    api_key=a\n    or "TESTop13", token="TESTop14")\n':
                f'f(\n    api_key={M}\n    or "{M}", token="{M}")\n',
            'c = Client(api_key=get(\n    "K")\n    or "TESTop15")\n':
                f'c = Client(api_key={M}(\n    "{M}")\n    or "{M}")\n',
            'API_KEY = os.getenv(\n    "K"\n) or\n    "TESTop16"\nx = 1\n':
                f'API_KEY = {M}(\n    "{M}"\n) or\n    "{M}"\nx = 1\n',
        }
        check(self, cases, eol="")

    def test_implicit_concatenation_and_lambda(self):
        """Склейка строк вплотную и через пробел — одно значение; у лямбды значение — её тело. Кавычка без строки
        перед ней по-прежнему закрывает внешнюю строку (юнит systemd)."""
        check(self, {
            'f(api_key="TESTgl1" "TESTgl2")': f"f(api_key={M})",
            'OpenAI(api_key="sk-or-v1-abc" "TESTgl3", timeout=5)': f"OpenAI(api_key={M}, timeout=5)",
            'h = {"token": "a" "TESTgl4"}': f'h = {{"token": {M}}}',
            'cfg["token"] = "a" "TESTgl5"': f'cfg["token"] = {M}',
            "def f(token='a' f'{x}TESTgl6'): pass": f"def f(token={M}): pass",
            'kwargs["token"] = kwargs.get("token") or "a" "TESTgl7"': f'kwargs["token"] = {M}',
            'Environment="TOKEN=TESTgl8" "MODE=1"': f'Environment="TOKEN={M}" "MODE=1"',
            'f(key=lambda: os.getenv("T") or "TESTlm1")': f"f(key={M})",
            'Thread(target=run, kwargs={"token": lambda t="x": t or "TESTlm2"})':
                f'Thread(target=run, kwargs={{"token": {M}}})',
        })

    def test_black_splits_concatenation_names_and_annotations(self):
        """Как black переносит значение внутри скобок: строка под значением-строкой — склейка (под вызовом — нет);
        пустая строка и комментарий между значением и продолжением; имя в кавычках одно на строке внутри вызова или
        индекса — значение за скобкой ниже; аннотация типа, перенесённая внутри скобок, — значение после «=»."""
        cases = {
            'f(\n    api_key="a"\n    "TESTgl9",\n    timeout=5,\n)\n':
                f'f(\n    api_key="{M}"\n    "{M}",\n    timeout=5,\n)\n',
            'h = {\n    "password": "a"  # половина\n    f"TESTgl10"\n    "TESTgl11",\n}\n':
                f'h = {{\n    "password": "{M}"  # половина\n    f"{M}"\n    "{M}",\n}}\n',
            'c = Client(\n    api_key=os.getenv("K")\n    # запасной\n\n    or "TESTgl12",\n)\n':
                f'c = Client(\n    api_key={M}\n    # запасной\n\n    or "{M}",\n)\n',
            'f(\n    key=lambda: os.getenv("T")\n    or "TESTlm3",\n)\n': f'f(\n    key={M}\n    or "{M}",\n)\n',
            'TOKEN = "a" """TESTgl13\nTESTgl14"""\nx = 1\n': f'TOKEN = "{M}" """{M}\n{M}"""\nx = 1\n',
            'token = os.getenv("T")\n"""Строка документации."""\n': f'token = {M}\n"""Строка документации."""\n',
            'os.environ[\n    "VK_TOKEN"\n] = "TESTnm1"\nx = 1\n': f'os.environ[\n    "VK_TOKEN"\n] = "{M}"\nx = 1\n',
            'bot = Bot(\n    os.environ.get(\n        "VK_TOKEN"\n    )\n    or "TESTnm2"\n)\n':
                f'bot = Bot(\n    os.environ.get(\n        "VK_TOKEN"\n    )\n    or "{M}"\n)\n',
            'x = os.environ.get(\n    "VK_TOKEN"\n) or "TESTnm3"\n':
                f'x = os.environ.get(\n    "VK_TOKEN"\n) or "{M}"\n',
            'NAMES = [\n    "token"\n]\nx = 1\n': 'NAMES = [\n    "token"\n]\nx = 1\n',
            'async def main(\n    api_key: Optional[\n        str\n    ] = "TESTan10",\n) -> None:\n    pass\n':
                f'async def main(\n    api_key: {M}[\n        {M}\n    ] = "{M}",\n) -> None:\n    pass\n',
        }
        check(self, cases, eol="")

    def test_argument_list_one_per_line(self):
        """Остаток списка аргументов за именем в кавычках, перенесённый по аргументу на строке (как у black и
        click/argparse): первая строка — значение, дальше — значения именованных аргументов (литералы остаются).
        В списке строк прячется только элемент за именем, как и на одной строке."""
        cases = {
            'parser.add_argument(\n    "--token",\n    "-t",\n    type=str,\n'
            '    default=os.getenv("VK_TOKEN", "TESTkw1"),\n    required=False,\n    help="ключ VK",\n)\nx = 1\n':
                f'parser.add_argument(\n    "--token",\n    "{M}",\n    type={M},\n    default={M},\n'
                f'    required=False,\n    help="{M}",\n)\nx = 1\n',
            'parser.add_argument("--token", type=str,\n                    default="TESTkw2")\n':
                f'parser.add_argument("--token", {M},\n                    default="{M}")\n',
            'p.add_argument(\n    "--api-key",\n    # из окружения\n\n    default="TESTkw3",\n)\n':
                f'p.add_argument(\n    "--api-key",\n    # из окружения\n\n    default="{M}",\n)\n',
            'os.environ.setdefault(\n    "VK_TOKEN",  # ключ группы\n    "TESTkw4",\n)\n':
                f'os.environ.setdefault(\n    "VK_TOKEN",  # ключ группы\n    "{M}",\n)\n',
            '__all__ = [\n    "SecretStr",\n    "Other",\n    "Third",\n]\n':
                f'__all__ = [\n    "SecretStr",\n    "{M}",\n    "Third",\n]\n',
            'KW = (\n    "abs", "token", "xyz",\n    "a", "b",\n)\n':
                f'KW = (\n    "abs", "token", "{M}",\n    "a", "b",\n)\n',
        }
        check(self, cases, eol="")

    def test_hash_inside_quotes_is_not_a_comment(self):
        """В присваивании до конца строки « #» внутри кавычек — часть значения, а не комментарий: значение целиком
        и с первого прохода (иначе повторная проверка дала бы ложную «ошибку», а хвост за « #» остался бы виден)."""
        check(self, {
            'key = line.split(" #")[0].strip()': f"key = {M}",
            'token = s.strip(" #")': f"token = {M}",
            '    token = token.gsub(symbol, " #{word} ")': f"    token = {M}",
            'token = os.getenv("T", "a #TESThash1")': f"token = {M}",
            "password = 'a #TESThash2' + b  # c": f"password = {M}  # c",
            'token = os.getenv("T")  # из окружения': f"token = {M}  # из окружения",
            "password: 'a #TESThash3' # c": f"password: '{M}' # c",
        })

    def test_operator_line_that_does_not_continue_value(self):
        """Строка с оператора или с if за значением в конце строки — не продолжение, если выражение за оператором
        не кончается разделителем или скобкой (сравнение, текст) или если это заголовок блока: код и текст
        остаются читаемыми, литерал в сравнении по-прежнему прячется. «If» и «OR» с заглавной — не запасное
        значение."""
        cases = {
            'token = os.getenv("T")\nif not token:\n    sys.exit(1)\n':
                f'token = {M}\nif not token:\n    sys.exit(1)\n',
            "key = (module, name)\nif (registry.get(key) == code and\n        inverted.get(code) == key):\n":
                f"key = {M}\nif (registry.get(key) == code and\n        inverted.get(code) == key):\n",
            'TOKEN=abc\nif [ -z "$TOKEN" ]; then\n': f'TOKEN={M}\nif [ -z "$TOKEN" ]; then\n',
            "    const key = normalize(option.id)\n    if (!key) continue\n":
                f"    const key = {M}\n    if (!key) continue\n",
            'if (key == "x"\n        and val is sentinel\n        and "backend" in self):\n':
                f'if (key == "{M}"\n        and val is sentinel\n        and "backend" in self):\n',
            'if (token == "a"\n        or token == "TESTop17"):\n':
                f'if (token == "{M}"\n        or token == "{M}"):\n',
            "key = f(key)\nif is_integer(key) or is_float(key):\n": f"key = {M}\nif is_integer(key) or {M}:\n",
            "export API_KEY=abc\n+ python3 run.py\n": f"export API_KEY={M}\n+ python3 run.py\n",
            "Tab key. If completekey is\nnot None and the module is available\n":
                "Tab key. If completekey is\nnot None and the module is available\n",
            "that only pass if\nthe call is the most recent one\n":
                "that only pass if\nthe call is the most recent one\n",
            'grep "auth OR signin" log\n': 'grep "auth OR signin" log\n',
        }
        check(self, cases, eol="")

    def test_name_value_pairs(self):
        check(self, {
            '<param name="api_key" value="TESTpair1"/>': f'<param name="api_key" value="{M}"/>',
            '<arg name="token" default="TESTpair2"/>': f'<arg name="token" default="{M}"/>',
            '<token>TESTpair3</token>': f"<token>{M}</token>",
            'SetEnvironmentVariable(name="VK_TOKEN", value="TESTpair4")':
                f'SetEnvironmentVariable(name="VK_TOKEN", value="{M}")',
            '{"name": "VK_TOKEN", "value": "TESTpair5"}': f'{{"name": "VK_TOKEN", "value": "{M}"}}',
            '<param name="mode" value="demo"/>': '<param name="mode" value="demo"/>',
            '<property name="connection.password">TESTpair9</property>':
                f'<property name="connection.password">{M}</property>',
            '<param name="mode">demo</param>': '<param name="mode">demo</param>',
        })
        cases = {
            "env:\n  - name: VK_TOKEN\n    value: TESTpair6\n  - name: MODE\n    value: demo\n":
                f"env:\n  - name: VK_TOKEN\n    value: {M}\n  - name: MODE\n    value: demo\n",
            'SetEnvironmentVariable(\n    name="VK_TOKEN",\n    value="TESTpair7",\n)\nvalue = 1\n':
                f'SetEnvironmentVariable(\n    name="VK_TOKEN",\n    value="{M}",\n)\nvalue = 1\n',
            'env = [\n    {"name": "API_KEY",\n     "value": "TESTpair8"},\n]\n':
                f'env = [\n    {{"name": "API_KEY",\n     "value": "{M}"}},\n]\n',
        }
        check(self, cases, eol="")

    def test_code_after_condition_stays_readable(self):
        for text in ("if api_key:\n    connect(url)\n", "if token:  # есть\n    keep()\n",
                     "def set_token(self, token):\n    self.ready = True\n", "if token: connect(url)\n",
                     "for key in keys: print(key)\n", "if token or password:\n    go()\n",
                     "x = [t for t in tokens if t]\n", "def f(self, token: str, timeout: int = 5):\n",
                     "def get_read_topic(topic_key: str) -> tuple[str, str]:\n",
                     "with open(p) as token_file: data = token_file.read()\n"):
            with self.subTest(text=text):
                self.assertEqual(lines(text), text)

    def test_json_inside_json_string(self):
        check(self, {
            '{"level":"debug","args":"{\\"api_key\\":\\"TESTesc1\\",\\"x\\":1}"}':
                f'{{"level":"debug","args":"{{\\"api_key\\":\\"{M}\\",\\"x\\":1}}"}}',
            'x \\\\\\"token\\\\\\":\\\\\\"TESTesc2\\\\\\" y': f'x \\\\\\"token\\\\\\":\\\\\\"{M}\\\\\\" y',
        })

    def test_header_value_with_any_scheme(self):
        check(self, {
            "DEBUG Authorization: token TESThdr1": f"DEBUG Authorization: {M}",
            "curl -H 'Authorization: OAuth TESThdr2' https://x": f"curl -H 'Authorization: {M}' https://x",
            "map[Authorization:[token TESThdr3] Accept:[*/*]]": f"map[Authorization:[{M}] Accept:[*/*]]",
            "< Set-Cookie: sid=TESThdr4; Path=/": f"< Set-Cookie: {M}",
            "send: b'GET / HTTP/1.1\\r\\nAuthorization: token TESThdr6\\r\\nHost: x\\r\\n'":
                f"send: b'GET / HTTP/1.1\\r\\nAuthorization: {M}\\r\\nHost: x\\r\\n'",
            'requests.get(u, auth=("robot", "TESThdr5"))': f"requests.get(u, auth={M})",
            'r = requests.post(URL, headers={"Authorization": "Bearer " + API_KEY}, json=body)':
                f'r = requests.post(URL, headers={{"Authorization": {M}}}, json=body)',
            '{"Cookie": "sid=" + sid, "X": 1}': f'{{"Cookie": {M}, "X": 1}}',
            "DEBUG Cookie: sid=TESThdr7; theme=dark": f"DEBUG Cookie: {M}",
        })


class JsonTest(unittest.TestCase):
    CONFIG = {
        "agents": {"defaults": {"max_tokens": 32768, "max_tool_iterations": 50, "summarize_token_percent": 75,
                                "subturn": {"default_token_budget": 0}}},
        "model_list": [{"model_name": "gpt", "api_key": "anything", "api_base": "https://routerai.ru/api/v1"}],
        "channels": {"telegram": {"enabled": True, "token": "x", "proxy": "http://u:p@proxy:3128"},
                     "mqtt": {"password": 31337424, "port": 1883}},
        "tools": {"mcp": {"servers": {"ros": {
            "command": "python3", "args": ["s.py", "--token", "abc", "--config", "c.json"],
            "env": {"OPENAI_API_KEY": "zz", "MODE": "x"}, "headers": {"Authorization": "Bearer q"}}}}},
        "gateway": {"hot_reload": False, "port": 18790},
    }

    def test_config_keys_and_args(self):
        got = json.loads(redact.redact_json(json.dumps(self.CONFIG)))
        self.assertEqual(got["model_list"][0]["api_key"], M)
        self.assertEqual(got["model_list"][0]["api_base"], "https://routerai.ru/api/v1")
        self.assertEqual(got["channels"]["telegram"]["token"], M)
        self.assertEqual(got["channels"]["telegram"]["proxy"], f"http://{M}@proxy:3128")
        self.assertEqual(got["channels"]["mqtt"], {"password": M, "port": 1883})
        ros = got["tools"]["mcp"]["servers"]["ros"]
        self.assertEqual(ros["args"], ["s.py", "--token", M, "--config", "c.json"])
        self.assertEqual(ros["env"], {"OPENAI_API_KEY": M, "MODE": "x"})
        self.assertEqual(ros["headers"], {"Authorization": M})
        self.assertEqual(got["agents"], self.CONFIG["agents"])
        self.assertEqual(got["gateway"], self.CONFIG["gateway"])

    def test_nested_values_under_secret_key(self):
        got = json.loads(redact.redact_json('{"credentials": {"user": "u", "list": ["a", 1, null, true]}}'))
        self.assertEqual(got, {"credentials": {"user": M, "list": [M, M, None, True]}})

    def test_pairs_lists_and_multiline_strings(self):
        src = {"env": [{"name": "VK_TOKEN", "value": "a"}, {"name": "MODE", "value": "demo"}],
               "argv": ["VK_TOKEN", "b", "--port", "1"], "script": "TOKEN = \\\n  'c'\nok = 1"}
        got = json.loads(redact.redact_json(json.dumps(src)))
        self.assertEqual(got["env"], [{"name": "VK_TOKEN", "value": M}, {"name": "MODE", "value": "demo"}])
        self.assertEqual(got["argv"], ["VK_TOKEN", M, "--port", "1"])
        self.assertEqual(got["script"], f"TOKEN = \\\n  '{M}'\nok = 1")

    def test_broken_json_falls_back_to_lines(self):
        self.assertEqual(redact.redact_json('{"api_key": "abc", \n'), f'{{"api_key": "{M}", \n')


class IdempotentTest(unittest.TestCase):
    def test_second_pass_changes_nothing(self):
        texts = [
            "export OPENAI_API_KEY=abc\nAuthorization: Bearer TESTbearer0123\napi_keys:\n  - a\n",
            '"api_keys": [\n  "a"\n],\napi_keys: ["a"]\n--api-key x mqtt://u:p@h\n',
            "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n",
            "?act=a_check&key=0123abcd&ts=1 /bot123456789:AAEhBP0av28Y3GjKFzZp8cQ7nT1xWvYq0aB/x\nMQTT_PASSWORD=123\n",
            'os.environ["VK_TOKEN"] = "a"\nos.environ.setdefault("API_KEY", "b")\ntoken = os.getenv("T", "c") or "d"\n',
            '{"args":"{\\"api_key\\":\\"e\\"}"}\nDEBUG Authorization: token f\ncurl -u u:g x\nTOKEN = (\n  "h"\n)\n',
            'sshpass -p i ssh -p 22 x\nsudo -S x <<< j\n<token>k</token>\n<p name="token" value="l"/>\n',
            '- name: VK_TOKEN\n  value: m\nAPI_KEY = \\\n  "n"\nq=%22token%22%3A%22o%22\nif token == "p": x\n',
            'r = post(URL, headers={"Authorization": "Bearer " + KEY}, json=b)\n{"Cookie": "sid=" + sid, "X": 1}\n',
            "credentials = {\n  'password': 'q',\n}\nTOKEN = '''r\ns'''\nTOKEN, ID = \"t\", 1\n",
            # маска, за которой скобка вызова, не закрытая на строке, и строка с вырезанным .format() за ней
            'os.environ["VK_TOKEN"] = "a" + str(\n    x)\ncall(api_key="a" + f(\n    x))\n',
            '{"Cookie": "sid=" + sid(\n    1), "X": 1}\n',
            'node, key, req = cfg.get("A".format(u)), "B".format(u), cfg.get(\n  "C")\n',
            'headers["proxy-authorization"] = "Basic " + b64encode(\n    b(x)).decode("latin-1")\n',
            "key = f(key)\nif is_integer(key) or is_float(key):\n    pass\n",
            # « #» в кавычках в присваивании до конца строки (Homebrew, cask/token_generator.rb)
            'key = line.split(" #")[0].strip()\ntoken = s.strip(" #")\n    token = token.gsub(symbol, " #{word} ")\n',
            'f(api_key="a" "b")\nf(\n    api_key="a"\n    # c\n    "b",\n)\n',
            'os.environ[\n    "K_TOKEN"\n] = (\n    "c"\n)\n',
            'async def f(\n    key: Optional[\n        str\n    ] = "d",\n): pass\n',
            'p.add_argument(\n    "--token",\n    default=os.getenv(\n        "T"\n    ),\n    help="x",\n)\n',
        ]
        for text in texts:
            once = lines(text)
            with self.subTest(text=text):
                self.assertEqual(lines(once), once)
        once = redact.redact_json(json.dumps(JsonTest.CONFIG))
        self.assertEqual(redact.redact_json(once), once)


class LongLineTest(unittest.TestCase):
    """Время — с запасом: при квадратичном поиске имени эти строки обрабатываются минутами."""

    def timed(self, line, want):
        start = time.monotonic()
        self.assertEqual(lines(line), want)
        self.assertLess(time.monotonic() - start, 10)

    def test_long_run_of_name_characters(self):
        self.timed("a" * 100000 + "_token=TESTlong\n", "a" * 100000 + f"_token={M}\n")

    def test_base64_image_in_log_line(self):
        line = '{"msg":"frame","data":"data:image/jpeg;base64,' + "QUJDRA" * 200000 + '"}\n'
        self.timed(line, line)

    def test_many_names_without_assignment(self):
        """Имена, от которых разбор отказывается (token, token, …; token or a; def f(token: (): при неограниченном
        поиске целей присваивания первая строка обрабатывается больше минуты."""
        for line in (", ".join(["token"] * 20000) + "\n", "x = " + "token or a " * 20000 + "\n",
                     "def f(" + "token: (, " * 5000 + "\n"):
            with self.subTest(line=line[:20]):
                self.timed(line, line)

    def test_many_assignments_in_one_line(self):
        line = " ".join("token=TESTmany%d" % i for i in range(20000)) + "\n"
        self.timed(line, " ".join(f"token={M}" for _ in range(20000)) + "\n")


class CliTest(unittest.TestCase):
    def run_cli(self, args, data):
        return subprocess.run([sys.executable, "-I", str(LAB / "redact.py"), *args], input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_stdin_keeps_non_utf8_bytes(self):
        done = self.run_cli([], b"\xff\xfe raw token=abc\r\nnext\n")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, b"\xff\xfe raw token=" + M.encode() + b"\r\nnext\n")

    def test_json_mode(self):
        done = self.run_cli(["--json"], b'{"api_key": "abc", "n": 1}')
        self.assertEqual(json.loads(done.stdout), {"api_key": M, "n": 1})

    def test_compiles_without_warnings(self):
        """redact.py запускается на каждую команду сбора: предупреждение компиляции («\\ » в строке) печаталось бы
        в stderr каждый раз, а в новых версиях Python станет ошибкой."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile((LAB / "redact.py").read_text(encoding="utf-8"), "redact.py", "exec")

    def test_bad_arguments(self):
        done = self.run_cli(["--nope"], b"")
        self.assertEqual(done.returncode, 2)
        self.assertIn(b"--check", done.stderr)

    def test_check_fixes_files_and_reports_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "clean.txt").write_text(f"api_key: {M}\n")
            (root / "sub" / "leak.log").write_text("key sk-TEST1234567890abcdef\n")
            (root / "c.json").write_text('{"token": "abc"}')
            done = self.run_cli(["--check", tmp], b"")
            self.assertEqual(done.returncode, 0)
            self.assertEqual(sorted(done.stdout.decode().split()), ["c.json", os.path.join("sub", "leak.log")])
            self.assertEqual((root / "sub" / "leak.log").read_text(), f"key {M}\n")
            self.assertEqual(json.loads((root / "c.json").read_text()), {"token": M})
            self.assertEqual(self.run_cli(["--check", tmp], b"").stdout, b"")

    def test_check_after_first_pass_reports_nothing(self):
        """Как в collect_diag.sh: первый проход при записи, повторный — --check; лишнее в выводе --check —
        строка «ошибка» в отчёте."""
        code = ('r = requests.post(URL, headers={"Authorization": "Bearer " + KEY}, json=body)\n'
                "credentials = {\n    'password': 'TESTcli1',\n}\n"
                'def f(token: str = "TESTcli2"):\n    TOKEN, ID = "TESTcli3", 1\n')
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_cli([], code.encode())
            Path(tmp, "worker.py").write_bytes(done.stdout)
            self.assertNotIn(b"TESTcli", done.stdout)
            self.assertEqual(self.run_cli(["--check", tmp], b"").stdout, b"")


if __name__ == "__main__":
    unittest.main()
