import json

import pytest

from jelly_yt.channel_list import ChannelListError, add_channel, read_channels


def test_append_dedupes_and_keeps_inode(tmp_path):
    path = tmp_path / "channel_list.json"
    path.write_text(json.dumps(["https://www.youtube.com/@leagueoflegends", "list-PLabc"], indent=4))
    inode = path.stat().st_ino

    assert add_channel(path, "https://www.youtube.com/@veritasium")
    assert not add_channel(path, "https://youtube.com/@Veritasium/")
    assert not add_channel(path, "https://www.youtube.com/@leagueoflegends/videos")

    assert read_channels(path) == [
        "https://www.youtube.com/@leagueoflegends", "list-PLabc", "https://www.youtube.com/@veritasium"]
    assert path.stat().st_ino == inode  # rewritten in place (bind-mount safe)


def test_missing_file_is_created(tmp_path):
    path = tmp_path / "sub" / "channel_list.json"
    assert add_channel(path, "https://www.youtube.com/@x")
    assert read_channels(path) == ["https://www.youtube.com/@x"]


def test_invalid_file_is_not_clobbered(tmp_path):
    path = tmp_path / "channel_list.json"
    path.write_text('{"not": "a list"}')
    with pytest.raises(ChannelListError):
        add_channel(path, "https://www.youtube.com/@x")
    assert path.read_text() == '{"not": "a list"}'
