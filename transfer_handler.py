# transfer_handler.py
from fpl_api import get_player_info

def match_transfers(players_out, players_in):
    """
    Match transferred-out players with transferred-in players by position.
    """
    transfer_pairs = []
    unmatched_in = players_in.copy()

    for out_player, out_position in players_out:
        matching_in_players = [
            (in_player, in_position) for in_player, in_position in unmatched_in
            if in_position == out_position
        ]

        if matching_in_players:
            matched_in = matching_in_players[0]
            transfer_pairs.append((out_player, out_position, matched_in[0], matched_in[1]))
            unmatched_in.remove(matched_in)

    return transfer_pairs, unmatched_in

def create_transfer_payload(transfer_pairs, team_id, event):
    """
    Create a payload for performing FPL transfers via the API.
    """
    transfers = []
    for out_player, out_position, in_player, in_position in transfer_pairs:
        out_player_info = get_player_info(out_player, out_position)
        in_player_info = get_player_info(in_player, in_position)

        if out_player_info and in_player_info:
            transfer = {
                "element_in": in_player_info['id'],
                "element_out": out_player_info['id'],
                "purchase_price": in_player_info['now_cost'],
                "selling_price": out_player_info['now_cost']
            }
            transfers.append(transfer)

    return {
        "confirmed": False,
        "entry": team_id,
        "event": event,
        "transfers": transfers,
        "wildcard": False,
        "freehit": False,
        "bench_boost": False,
        "triple_captain": False
    }
