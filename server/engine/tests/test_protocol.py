from bionico import protocol


def test_command_for_registers_protocol_open():
    cmd = protocol.command_for(r"C:\Bionico\bionico.exe")
    assert cmd == r'"C:\Bionico\bionico.exe" protocol-open "%1"'


def test_open_uri_rejects_other_schemes():
    assert protocol.open_uri("https://example.com/restart") == 2
