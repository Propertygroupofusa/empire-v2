"""The chart panel shows what the grid bot is waiting for, and can only read.

The owner, 2026-10-14: "I don't have a grid like this that's showing me what
the bots are doing ... how the bots are literally deciding what they're going
to do with my money." The arithmetic is pinned in test_bot_levels.js; this pins
the wiring on the dashboard page.
"""
import re

HTML = open("family_tree_dashboard.html", encoding="utf-8").read()
JS = open("static/advanced_charts.js", encoding="utf-8").read()
FN = HTML[HTML.index("async function advBotLevels("):HTML.index("async function advDraw(")]


def test_the_card_sits_right_under_the_price_chart():
    i_price = HTML.index('<canvas id="adv-price"')
    i_card = HTML.index('id="adv-bot-card"')
    i_vol = HTML.index('<canvas id="adv-volume"')
    assert i_price < i_card < i_vol


def test_the_card_says_it_is_read_only():
    card = HTML[HTML.index('id="adv-bot-card"'):HTML.index('<canvas id="adv-volume"')]
    assert "Read-only" in card and "places, sizes and cancels nothing" in card


def test_it_reads_the_same_fleet_status_the_page_already_uses():
    assert "apiGet('/grid-status')" in FN


def test_it_has_its_own_try_so_a_failed_read_cannot_blank_the_chart():
    assert "try {" in FN and "catch (e)" in FN
    assert "says nothing about what the bot will do" in FN


def test_the_levels_are_passed_to_the_price_chart():
    assert re.search(r"ADV\.drawPrice\(document\.getElementById\('adv-price'\),\s*candles,\s*bot \? bot\.lines : null\)", HTML)


def test_a_coin_with_no_bot_says_so_instead_of_drawing_nothing_silently():
    assert "No grid bot trades" in FN


def test_the_panel_cannot_write_anything():
    for bad in ("postGuarded", "method: 'POST'", 'method: "POST"', "writeHeaders", "x-dashboard-token"):
        assert bad not in FN, bad


def test_the_chart_module_has_no_network_or_order_call():
    assert not re.search(r"fetch\(|XMLHttpRequest|postGuarded|/api/", JS)


def test_there_is_a_legend_for_the_three_kinds_of_line():
    card = HTML[HTML.index('id="adv-bot-card"'):HTML.index('<canvas id="adv-volume"')]
    assert "buy level" in card and "sell level" in card and "rungs it holds" in card
