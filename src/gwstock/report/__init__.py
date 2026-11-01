"""Księga, raport dzienny i bramka L1 (spec §10, §11): `python -m gwstock.report`.

Moduły: `ledger` (eksport CSV i podsumowanie księgi), `gates` (status bramki L1 z danych dziennika), `cli` (raport).
Raport tylko czyta dziennik i niczego nie zmienia; nie tworzy `data/L1_PASSED` (robi to wyłącznie właściciel).
"""
