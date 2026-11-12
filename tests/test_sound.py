"""The wrong-window pop-up's chime: made here (no audio file in the repo), played wherever the board can."""
import wave
from pathlib import Path

from ctxswitch.client import sound


def test_the_eep_is_a_short_mono_wav_made_on_first_use(tmp_path):
    p = sound.sound_file("eep", root=tmp_path)
    assert p.exists() and p.name == f"eep-v{sound.VERSION}.wav"
    with wave.open(str(p)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, sound.RATE)
        assert 0.2 < w.getnframes() / w.getframerate() < 0.5            # a chirp, not a ringtone
    mtime = p.stat().st_mtime_ns
    assert sound.sound_file("eep", root=tmp_path).stat().st_mtime_ns == mtime   # cached, not remade


def test_it_rises_like_an_eep():
    s = sound.eep_samples()
    half = len(s) // 2
    crossings = lambda xs: sum(1 for a, b in zip(xs, xs[1:]) if a < 0 <= b)
    assert crossings(s[half:]) / (len(s) - half) > crossings(s[:half]) / half   # higher pitch in the second half


def test_on_wsl_windows_plays_it_through_the_wsl_share(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    argv = sound.player(Path("/home/me/.local/state/ctxswitch/sounds/eep-v1.wav"), wsl=True,
                        which=lambda c: "/mnt/c/x/powershell.exe" if c == "powershell.exe" else None)
    assert argv[0] == "/mnt/c/x/powershell.exe" and "-NoProfile" in argv
    assert "\\\\wsl.localhost\\Ubuntu-24.04\\home\\me\\.local\\state\\ctxswitch\\sounds\\eep-v1.wav" in argv[-1]
    assert "Media.SoundPlayer" in argv[-1] and "PlaySync" in argv[-1]


def test_elsewhere_the_first_player_found_and_none_is_silence():
    p = Path("/tmp/eep.wav")
    assert sound.player(p, wsl=False, which=lambda c: "/usr/bin/afplay" if c == "afplay" else None) == ["/usr/bin/afplay", str(p)]
    assert sound.player(p, wsl=False, which=lambda c: "/usr/bin/aplay" if c == "aplay" else None) == ["/usr/bin/aplay", "-q", str(p)]
    assert sound.player(p, wsl=False, which=lambda c: None) is None
    assert sound.player(p, wsl=True, which=lambda c: None, exists=lambda x: False) is None   # WSL with no interop


def test_play_never_waits_and_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(sound, "SOUNDS", tmp_path)
    started = []
    monkeypatch.setattr(sound, "player", lambda path, **kw: ["play", str(path)])
    assert sound.play("eep", popen=lambda argv, **kw: started.append((argv, kw))) is True
    assert started and started[0][1]["start_new_session"] is True and started[0][1]["stdout"] is not None
    assert sound.play("", popen=lambda *a, **k: 1 / 0) is False                       # "" = silent
    assert sound.play("eep", popen=lambda *a, **k: 1 / 0) is False                    # a broken player: no error
