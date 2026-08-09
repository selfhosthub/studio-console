from studio_console import tui


class TestPhysicalRows:
    def _cols(self, monkeypatch, n):
        import os
        monkeypatch.setattr(
            tui.shutil, "get_terminal_size", lambda: os.terminal_size((n, 24))
        )

    def test_short_lines_count_once(self, monkeypatch):
        self._cols(monkeypatch, 80)
        assert tui._physical_rows(["a", "b", "c"]) == 3

    def test_wrapped_line_counts_its_rows(self, monkeypatch):
        self._cols(monkeypatch, 10)
        assert tui._physical_rows(["x" * 25]) == 3

    def test_exact_width_is_one_row(self, monkeypatch):
        self._cols(monkeypatch, 10)
        assert tui._physical_rows(["x" * 10]) == 1

    def test_ansi_codes_do_not_count(self, monkeypatch):
        self._cols(monkeypatch, 10)
        line = "\033[1m" + "x" * 10 + "\033[0m"
        assert tui._physical_rows([line]) == 1

    def test_empty_line_is_one_row(self, monkeypatch):
        self._cols(monkeypatch, 80)
        assert tui._physical_rows([""]) == 1
