from jelly_yt.ytdlp import AuthRequired, VideoUnavailable, YtDlpError, classify_error


def test_error_classification():
    private = "ERROR: [youtube] x: Private video. Sign in if you've been granted access. Use --cookies"
    assert isinstance(classify_error(Exception(private)), VideoUnavailable)
    bot = "ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies-from-browser"
    assert isinstance(classify_error(Exception(bot)), AuthRequired)
    assert isinstance(classify_error(Exception("HTTP Error 429: Too Many Requests")), AuthRequired)
    assert isinstance(classify_error(Exception("Video unavailable")), VideoUnavailable)
    assert type(classify_error(Exception("Unable to connect to proxy"))) is YtDlpError
