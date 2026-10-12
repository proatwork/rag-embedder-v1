"""Tests of the one place the scripts take a machine's addresses, ports and keys from (settings.py).

No test reads this machine's own settings.env: each names the file it means, or says there is none.
Run from the project folder: venvs/bin/python -m unittest discover -s tests -v
"""

import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import settings

SERVER = "http://embedding.example:1234"
OTHER_SERVER = "http://chat.example:8000/v1"
MODEL = "some/model"
KEY = "k" * 32
NO_FILE = {settings.FILE_VARIABLE: ""}  # an environment that says there is no settings file


class FileCase(unittest.TestCase):
    """Tests that write a settings file of their own."""

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "settings.env"

    def written(self, text: str) -> dict[str, str]:
        """An environment that names a settings file holding `text`."""
        self.file.write_text(text, encoding=settings.TEXT_ENCODING)
        return {settings.FILE_VARIABLE: str(self.file)}


class ReadFileTest(FileCase):
    def read(self, text: str) -> dict[str, str]:
        self.written(text)
        return settings.read_file(self.file)

    def test_a_line_is_a_name_and_its_value(self):
        self.assertEqual(self.read(f"{settings.EMBEDDING_SERVER}={SERVER}\n{settings.MCP_PORT} = 9000 \n"), {settings.EMBEDDING_SERVER: SERVER, settings.MCP_PORT: "9000"})

    def test_comments_and_empty_lines_are_skipped(self):
        self.assertEqual(self.read(f"# the server\n\n   # indented\n{settings.CHAT_MODEL}={MODEL}\n"), {settings.CHAT_MODEL: MODEL})

    def test_quotes_around_a_value_are_dropped_and_export_is_allowed(self):
        self.assertEqual(self.read(f'export A="{SERVER}"\nB=\'{MODEL}\'\nC="open\nD=""\n'), {"A": SERVER, "B": MODEL, "C": '"open', "D": ""})

    def test_a_value_keeps_what_follows_its_first_equals_sign_and_any_hash(self):
        self.assertEqual(self.read("A=b=c#d\n"), {"A": "b=c#d"})

    def test_a_name_set_twice_has_its_last_value(self):
        self.assertEqual(self.read("A=1\nA=2\n"), {"A": "2"})

    def test_a_line_that_is_neither_stops_with_its_number_and_not_its_text(self):
        for bad in ("just words", "=secret", "two words=1", "9name=1"):
            with self.assertRaises(SystemExit) as stopped:
                self.read(f"A=1\n{bad}\n")
            self.assertRegex(str(stopped.exception), re.escape(str(self.file)) + ", line 2: not NAME=value")
            self.assertNotIn(bad, str(stopped.exception))  # the line may hold a key

    def test_no_file_is_no_settings(self):
        self.assertEqual(settings.read_file(self.file), {})

    def test_a_file_that_is_not_text_stops(self):
        self.file.write_bytes(b"\xff\xfe\x00A=1")
        with self.assertRaisesRegex(SystemExit, "cannot read the settings file"):
            settings.read_file(self.file)


class SettingsFileTest(unittest.TestCase):
    def test_the_file_beside_the_scripts_is_read_unless_another_is_named(self):
        self.assertEqual(settings.settings_file({}), settings.SETTINGS_FILE)
        self.assertEqual(settings.SETTINGS_FILE.parent, Path(settings.__file__).parent)
        self.assertEqual(settings.settings_file({settings.FILE_VARIABLE: " /elsewhere/x.env "}), Path("/elsewhere/x.env"))

    def test_an_empty_name_means_no_file(self):
        self.assertIsNone(settings.settings_file(NO_FILE))
        self.assertEqual(settings.everything(NO_FILE), NO_FILE)

    def test_without_an_environment_given_the_process_own_is_used(self):
        with mock.patch.dict(os.environ, {**NO_FILE, settings.CHAT_MODEL: MODEL}):
            self.assertIsNone(settings.settings_file())
            self.assertEqual(settings.chat_model(), MODEL)


class LookupTest(FileCase):
    def test_the_environment_wins_over_the_file_and_the_file_over_the_default(self):
        environment = self.written(f"{settings.EMBEDDING_SERVER}={SERVER}\n{settings.CHAT_MODEL}={MODEL}\n")
        self.assertEqual(settings.embedding_server(environment), SERVER)
        self.assertEqual(settings.embedding_server({**environment, settings.EMBEDDING_SERVER: OTHER_SERVER}), OTHER_SERVER)
        self.assertEqual(settings.chat_model(environment), MODEL)
        self.assertEqual(settings.mcp_port(environment), settings.DEFAULT_MCP_PORT)

    def test_a_name_set_to_nothing_keeps_its_default_or_the_files_value(self):
        environment = self.written(f"{settings.EMBEDDING_SERVER}=\n{settings.CHAT_MODEL}={MODEL}\n")
        self.assertEqual(settings.embedding_server(environment), settings.LOCAL_SERVER)
        self.assertEqual(settings.setting(settings.CHAT_MODEL, {**environment, settings.CHAT_MODEL: "  "}), "")  # the environment wins, also with nothing
        self.assertEqual(settings.chat_model({**environment, settings.CHAT_MODEL: "  "}), settings.DEFAULT_CHAT_MODEL)

    def test_the_file_is_not_put_into_the_environment(self):
        environment = self.written(f"{settings.MCP_KEY}={KEY}\n")
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(settings.setting(settings.MCP_KEY), KEY)
            self.assertNotIn(settings.MCP_KEY, os.environ)

    def test_any_name_can_be_kept_in_the_file(self):
        self.assertEqual(settings.setting("MY_CHAT_KEY", self.written(f"MY_CHAT_KEY= {KEY} \n")), KEY)
        self.assertEqual(settings.setting("NOT_THERE", self.written("")), "")


class DefaultsTest(unittest.TestCase):
    """What a machine without any settings gets: everything on the machine itself."""

    def test_the_servers_are_this_machine_and_the_chat_server_is_the_embedding_server(self):
        self.assertEqual(settings.embedding_server(NO_FILE), settings.LOCAL_SERVER)
        self.assertEqual(settings.chat_server(NO_FILE), settings.LOCAL_SERVER)
        self.assertRegex(settings.LOCAL_SERVER, r"^http://127\.0\.0\.1:\d+$")

    def test_the_chat_server_follows_the_embedding_server_until_it_is_set_itself(self):
        self.assertEqual(settings.chat_server({**NO_FILE, settings.EMBEDDING_SERVER: SERVER}), SERVER)
        self.assertEqual(settings.chat_server({**NO_FILE, settings.EMBEDDING_SERVER: SERVER, settings.CHAT_SERVER: OTHER_SERVER}), OTHER_SERVER)

    def test_the_model_and_the_ports(self):
        self.assertEqual(settings.chat_model(NO_FILE), settings.DEFAULT_CHAT_MODEL)
        self.assertEqual((settings.chatbot_port(NO_FILE), settings.mcp_port(NO_FILE)), (settings.DEFAULT_CHATBOT_PORT, settings.DEFAULT_MCP_PORT))
        self.assertNotEqual(settings.DEFAULT_CHATBOT_PORT, settings.DEFAULT_MCP_PORT)


class PortTest(unittest.TestCase):
    def test_a_port_that_is_set_is_the_number(self):
        self.assertEqual(settings.mcp_port({**NO_FILE, settings.MCP_PORT: " 9000 "}), 9000)
        self.assertEqual(settings.chatbot_port({**NO_FILE, settings.CHATBOT_PORT: str(settings.MAX_PORT)}), settings.MAX_PORT)

    def test_anything_but_a_port_stops_and_names_the_setting(self):
        for bad in ("0", "-1", "65536", "80.5", "http", "8 0", "٣٤"):
            with self.assertRaisesRegex(SystemExit, settings.MCP_PORT + ".*not a port"):
                settings.mcp_port({**NO_FILE, settings.MCP_PORT: bad})


class ExampleFileTest(unittest.TestCase):
    """The pattern that is committed (settings.example.env): it names every setting and holds no value."""

    def setUp(self):
        self.example = settings.read_file(settings.EXAMPLE_FILE)

    def test_it_names_every_setting(self):
        names = {value for name, value in vars(settings).items() if name.isupper() and isinstance(value, str) and value.startswith("NORMS_")} - {settings.FILE_VARIABLE}
        self.assertEqual(set(self.example), names)

    def test_it_holds_no_value_so_that_copying_it_changes_nothing_and_gives_nothing_away(self):
        self.assertEqual(set(self.example.values()), {""})
        self.assertNotRegex(settings.EXAMPLE_FILE.read_text(encoding=settings.TEXT_ENCODING), r"192\.168\.|\b10\.\d+\.\d+\.\d+")

    def test_the_defaults_it_states_are_the_ones_of_the_code(self):
        text = settings.EXAMPLE_FILE.read_text(encoding=settings.TEXT_ENCODING)
        for default in (settings.LOCAL_SERVER, settings.DEFAULT_CHAT_MODEL, str(settings.DEFAULT_CHATBOT_PORT), str(settings.DEFAULT_MCP_PORT)):
            self.assertIn(f"Default: {default}", text)


class NothingHardcodedTest(unittest.TestCase):
    """No script keeps an address of somebody's network: such an address belongs in settings.env."""

    PRIVATE_ADDRESS = re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}:\d+")  # with a port: an article is numbered "10.2.3.10" too

    def test_the_scripts_and_the_page_name_no_private_address(self):
        project = Path(settings.__file__).parent
        files = [*project.glob("*.py"), *(project / "chatbot").glob("*.py"), *(project / "chatbot").glob("*.html"), *(project / "chatbot").glob("*.js")]
        self.assertGreater(len(files), 10)
        found = {file.name: self.PRIVATE_ADDRESS.findall(file.read_text(encoding=settings.TEXT_ENCODING)) for file in files}
        self.assertEqual({name: addresses for name, addresses in found.items() if addresses}, {})


if __name__ == "__main__":
    unittest.main()
