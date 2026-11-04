"""Księga, raport dzienny i bramka L1 (spec §10, §11): `python -m gwstock.report`.

Moduły: `ledger` (eksport CSV, księga z walutą i dokładne sumy; wiersze instrumentu w walucie obcej mają kwoty w
walucie instrumentu, a kwota w EUR jest w rozliczeniu dziennika), `gates` (status bramki L1 z danych dziennika, także
per pasmo), `cli` (raport z osobnymi sekcjami pasm, gdy dziennik ma więcej niż jedno).
Raport tylko czyta dziennik i niczego nie zmienia; nie tworzy `data/L1_PASSED` (robi to wyłącznie właściciel).
"""
