"""Standalone Trello REST API helper used by the orchestrator."""

from typing import Dict, List

import requests


class TrelloAPI:
    """Lightweight wrapper around the Trello REST API."""

    BASE = "https://api.trello.com/1"

    def __init__(self, api_key: str, token: str, debug: bool = False):
        self.auth = {'key': api_key, 'token': token}
        self.debug = debug

    def _dbg(self, msg: str):
        if self.debug:
            print(f"[ORCH-TRELLO] {msg}")

    # -- cards ----------------------------------------------------------------

    def get_card(self, card_id: str) -> Dict:
        url = f"{self.BASE}/cards/{card_id}"
        params = {**self.auth, 'fields': 'id,name,desc,idList,idBoard'}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_cards_on_list(self, list_id: str) -> List[Dict]:
        url = f"{self.BASE}/lists/{list_id}/cards"
        params = {**self.auth, 'fields': 'id,name,desc,idList,dateLastActivity'}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_card_attachments(self, card_id: str) -> List[Dict]:
        url = f"{self.BASE}/cards/{card_id}/attachments"
        params = {**self.auth, 'fields': 'id,name,url,mimeType,bytes'}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, card_id: str, text: str):
        url = f"{self.BASE}/cards/{card_id}/actions/comments"
        params = {**self.auth, 'text': text}
        resp = requests.post(url, params=params, timeout=30)
        resp.raise_for_status()

    def get_card_comments(self, card_id: str) -> List[Dict]:
        url = f"{self.BASE}/cards/{card_id}/actions"
        params = {**self.auth, 'filter': 'commentCard'}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def move_card(self, card_id: str, list_id: str):
        url = f"{self.BASE}/cards/{card_id}"
        params = {**self.auth, 'idList': list_id}
        resp = requests.put(url, params=params, timeout=30)
        resp.raise_for_status()

    # -- lists ----------------------------------------------------------------

    def create_list(self, board_id: str, name: str) -> Dict:
        url = f"{self.BASE}/boards/{board_id}/lists"
        params = {**self.auth, 'name': name}
        resp = requests.post(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def create_card(self, list_id: str, name: str, desc: str = "") -> Dict:
        url = f"{self.BASE}/cards"
        params = {**self.auth, 'idList': list_id, 'name': name, 'desc': desc}
        resp = requests.post(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def archive_list(self, list_id: str):
        url = f"{self.BASE}/lists/{list_id}/closed"
        params = {**self.auth, 'value': 'true'}
        resp = requests.put(url, params=params, timeout=30)
        resp.raise_for_status()
