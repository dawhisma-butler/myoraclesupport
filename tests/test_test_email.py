"""opatchbot test-email: step-by-step SMTP diagnosis against a tiny local fake relay."""
import socket
import threading

from opatchbot import cli
from opatchbot.config import NotifyConfig
from opatchbot.notify import send_test_email
from tests.test_cli import svc  # noqa: F401  (fixture)


def fake_relay(deny=()):
    """One-connection SMTP server: accepts everything except RCPT TO addresses in `deny` (550 relaying denied)."""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5)
    got = {"data": b""}

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            f = c.makefile("rb")
            c.sendall(b"220 relay.test ESMTP\r\n")
            while True:
                line = f.readline()
                if not line:
                    break
                cmd = line.decode().strip()
                up = cmd.upper()
                if up.startswith("EHLO"):
                    c.sendall(b"250-relay.test\r\n250 OK\r\n")
                elif up.startswith("MAIL FROM"):
                    c.sendall(b"250 2.1.0 Sender OK\r\n")
                elif up.startswith("RCPT TO"):
                    addr = cmd.split(":", 1)[1].strip("<> ")
                    c.sendall(b"550 5.7.1 Relaying denied\r\n" if addr in deny else b"250 2.1.5 Recipient OK\r\n")
                elif up == "DATA":
                    c.sendall(b"354 go ahead\r\n")
                    while (l := f.readline()) not in (b".\r\n", b""):
                        got["data"] += l
                    c.sendall(b"250 2.0.0 queued as ABC123\r\n")
                elif up == "QUIT":
                    c.sendall(b"221 bye\r\n"); break
                else:
                    c.sendall(b"250 OK\r\n")
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, got


def test_delivered_to_relay():
    srv, got = fake_relay()
    ok, lines = send_test_email(NotifyConfig("127.0.0.1", srv.getsockname()[1], "opatchbot@x.edu", ["a@x.edu"]))
    srv.close()
    assert ok and any("[ok] DATA: 250 2.0.0 queued as ABC123" in l for l in lines)
    assert b"Subject: [opatchbot] test email from" in got["data"]


def test_relaying_denied_is_reported():
    srv, _ = fake_relay(deny={"a@x.edu"})
    ok, lines = send_test_email(NotifyConfig("127.0.0.1", srv.getsockname()[1], "opatchbot@x.edu", ["a@x.edu"]))
    srv.close()
    assert not ok and any("[FAIL] RCPT TO a@x.edu: 550 5.7.1 Relaying denied" in l for l in lines)


def test_bad_host_and_closed_port():
    ok, lines = send_test_email(NotifyConfig("no-such-host.invalid", 25, "a@b", ["c@d"]))
    assert not ok and "[FAIL] DNS" in lines[-1]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    ok, lines = send_test_email(NotifyConfig("127.0.0.1", port, "a@b", ["c@d"]), timeout=3)
    assert not ok and "[FAIL] connect" in lines[-1]


def test_cli_test_email_to_override(svc, capsys):
    srv, _ = fake_relay()
    svc.cfg.notify.smtp_host, svc.cfg.notify.smtp_port = "127.0.0.1", srv.getsockname()[1]
    assert cli.main(["test-email", "--to", "x@y.edu"], services=svc) == 0
    srv.close()
    assert "RCPT TO x@y.edu" in capsys.readouterr().out
