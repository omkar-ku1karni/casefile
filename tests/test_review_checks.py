from casefile.agents import quote_found


def test_quote_found_exact_and_whitespace():
    doc = "Date of loss: 10/25/2026\nTime of loss: 12:50 AM"
    assert quote_found("Date of loss: 10/25/2026", doc)
    assert quote_found("date of loss:  10/25/2026 time of loss", doc)


def test_quote_found_rejects_paraphrase():
    doc = "Another vehicle struck the back of my car while I was waiting to turn left onto Oak Ave."
    assert not quote_found("A truck slammed into the front of my car on the highway", doc)
    assert quote_found("struck the back of my car ... turn left onto Oak Ave", doc)
