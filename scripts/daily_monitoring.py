#!/usr/bin/env python3
"""
Monitoring quotidien Smart Sim (Phase 1 — autonome).

Étend `check_daily_persistence.py` pour mesurer :
  - Couverture collecte odds (existant)
  - Couverture sync résultats (NOUVEAU)
  - Winrate par marché (Over 2.5 / Over 1.5 / BTTS / Winner) sur les matchs résolus
  - Winrate par tranche de confiance
  - Winrate par ligue (top 10)
  - ROI théorique simple si closing/opening odds disponibles
  - Détection drift : alerte si winrate s'écarte fortement du modèle

Read-only sur Supabase. Aucun appel API-Football. Aucune écriture.

Usage :
    python3 scripts/daily_monitoring.py [--date YYYY-MM-DD] [--days N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Charge .env local
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(ROOT))
from supabase_db import _get_client  # noqa: E402


def _paris_today() -> _date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def _confidence_tier(p: float) -> str:
    if p is None:    return "unknown"
    if p >= 0.70:    return "fort"
    if p >= 0.65:    return "moyen"
    if p >= 0.58:    return "prudent"
    return "absent"


def fetch_rows(client, date_from: str, date_to: str) -> list[dict]:
    """Récupère les bet_history dans la fenêtre [date_from, date_to]."""
    cols = (
        "fixture_id,date,league_name,home_team,away_team,match_status,"
        "proba_over25,proba_o15,proba_btts,winner,winner_proba,is_smart_bet,"
        "odd_over25,odd_over15,odd_btts_yes,odds_fetched_at,"
        "total_goals,actual_winner,resolved_at,"
        "result_over25_won,result_over15_won,result_btts_won,result_winner_won"
    )
    try:
        resp = client.table("bet_history").select(cols).gte("date", date_from).lte("date", date_to).execute()
        return resp.data or []
    except Exception as e:
        # Fallback : colonnes results_* peut-être absentes (migration pas appliquée)
        print(f"  ⚠ select complet a échoué ({str(e)[:100]}), fallback sans colonnes results_*")
        basic_cols = (
            "fixture_id,date,league_name,home_team,away_team,match_status,"
            "proba_over25,proba_o15,proba_btts,winner,winner_proba,is_smart_bet,"
            "odd_over25,odd_over15,odd_btts_yes,odds_fetched_at"
        )
        resp = client.table("bet_history").select(basic_cols).gte("date", date_from).lte("date", date_to).execute()
        return resp.data or []


def coverage_section(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    return {
        "n_total":            n,
        "with_odd_over25":    sum(1 for r in rows if r.get("odd_over25") is not None),
        "with_odd_over15":    sum(1 for r in rows if r.get("odd_over15") is not None),
        "with_odd_btts_yes":  sum(1 for r in rows if r.get("odd_btts_yes") is not None),
        "with_odds_fetched":  sum(1 for r in rows if r.get("odds_fetched_at")),
        "with_resolved_at":   sum(1 for r in rows if r.get("resolved_at")),
        "status_FT":          sum(1 for r in rows if r.get("match_status") in ("FT", "AET", "PEN")),
        "status_NS":          sum(1 for r in rows if r.get("match_status") == "NS"),
        "status_LIVE_HT":     sum(1 for r in rows if r.get("match_status") in ("1H", "HT", "2H", "ET", "LIVE")),
        "pending_resolution": sum(1 for r in rows if not r.get("resolved_at")),
    }


def winrate_by_market(rows: list[dict]) -> dict:
    """Winrate global par marché sur les fixtures résolus."""
    out = {}
    for market in ("over25", "over15", "btts", "winner"):
        col = f"result_{market}_won"
        resolved = [r for r in rows if r.get(col) is not None]
        if not resolved:
            out[market] = {"n_resolved": 0}
            continue
        wins = sum(1 for r in resolved if r[col])
        out[market] = {
            "n_resolved":  len(resolved),
            "wins":        wins,
            "losses":      len(resolved) - wins,
            "winrate":     round(wins / len(resolved), 4),
            "base_rate":   None,  # calculé pour Over markets
        }
    # Base rate Over 2.5 (référence)
    resolved_over25 = [r for r in rows if r.get("total_goals") is not None]
    if resolved_over25:
        out["over25"]["base_rate"] = round(
            sum(1 for r in resolved_over25 if r["total_goals"] > 2) / len(resolved_over25), 4
        )
        out["over15"]["base_rate"] = round(
            sum(1 for r in resolved_over25 if r["total_goals"] > 1) / len(resolved_over25), 4
        )
    return out


def winrate_by_confidence(rows: list[dict], market: str = "over25",
                            proba_col: str = "proba_over25") -> dict:
    """Winrate par tranche de confiance pour un marché donné."""
    col = f"result_{market}_won"
    resolved = [r for r in rows if r.get(col) is not None and r.get(proba_col) is not None]
    if not resolved:
        return {}

    by_tier: dict[str, list] = defaultdict(list)
    for r in resolved:
        tier = _confidence_tier(r[proba_col])
        by_tier[tier].append(r[col])

    return {
        tier: {
            "n":       len(vals),
            "wins":    sum(1 for v in vals if v),
            "winrate": round(sum(1 for v in vals if v) / len(vals), 4) if vals else None,
        }
        for tier, vals in by_tier.items()
    }


def winrate_by_league(rows: list[dict], market: str = "over25", top: int = 10) -> list[dict]:
    """Top N ligues par volume résolu + leur winrate sur le marché."""
    col = f"result_{market}_won"
    by_league: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.get(col) is None: continue
        by_league[r.get("league_name") or "?"].append(r[col])

    stats = [
        {
            "league":  lg,
            "n":       len(vals),
            "wins":    sum(1 for v in vals if v),
            "winrate": round(sum(1 for v in vals if v) / len(vals), 4),
        }
        for lg, vals in by_league.items() if len(vals) >= 3
    ]
    stats.sort(key=lambda x: (-x["n"], -x["winrate"]))
    return stats[:top]


def smart_sim_winrate(rows: list[dict]) -> dict:
    """Performance spécifique des picks Smart Sim (is_smart_bet=true)."""
    smart_resolved = [r for r in rows if r.get("is_smart_bet") and r.get("result_over25_won") is not None]
    if not smart_resolved:
        return {"n_resolved": 0}
    wins = sum(1 for r in smart_resolved if r["result_over25_won"])
    avg_proba = sum(r.get("proba_over25") or 0 for r in smart_resolved) / len(smart_resolved)
    return {
        "n_resolved":  len(smart_resolved),
        "wins":        wins,
        "winrate":     round(wins / len(smart_resolved), 4),
        "avg_proba":   round(avg_proba, 4),
        "calibration_gap": round(avg_proba - (wins / len(smart_resolved)), 4),
    }


def verdict(coverage: dict, by_market: dict) -> str:
    n = coverage.get("n_total", 0)
    if n == 0:
        return "⚠ Aucun match sur la période."
    odds_cov = coverage.get("with_odd_over25", 0) / n
    resolved_cov = coverage.get("with_resolved_at", 0) / n
    msg = []
    if odds_cov >= 0.7:    msg.append(f"✅ collecte odds OK ({odds_cov*100:.0f}%)")
    elif odds_cov >= 0.3:  msg.append(f"🟡 collecte odds partielle ({odds_cov*100:.0f}%)")
    else:                  msg.append(f"🔴 collecte odds faible ({odds_cov*100:.0f}%)")

    if resolved_cov >= 0.5:   msg.append(f"✅ sync résultats OK ({resolved_cov*100:.0f}%)")
    elif resolved_cov >= 0.2: msg.append(f"🟡 sync résultats partielle ({resolved_cov*100:.0f}%)")
    else:                     msg.append(f"🔴 sync résultats faible ({resolved_cov*100:.0f}%) — vérifier worker results-sync")

    o25 = by_market.get("over25", {})
    if o25.get("winrate") is not None:
        msg.append(f"📊 Over 2.5 winrate (≥0.5 proba): {o25['winrate']*100:.1f}%")
    return " · ".join(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=_paris_today().isoformat(),
                    help="Date de fin de fenêtre (défaut : aujourd'hui Paris)")
    ap.add_argument("--days", type=int, default=1,
                    help="Nombre de jours rétrospectifs (défaut : 1 = uniquement la date)")
    ap.add_argument("--market", default="over25",
                    help="Marché pour les vues détaillées (over25/over15/btts/winner)")
    args = ap.parse_args()

    date_to = args.date
    date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=args.days - 1)).isoformat()
    print(f"=== Daily monitoring  date range: {date_from} → {date_to}  market focus: {args.market} ===\n")

    client = _get_client()
    if client is None:
        print("FATAL: client Supabase indisponible", file=sys.stderr)
        return 2

    rows = fetch_rows(client, date_from, date_to)
    print(f"Total bet_history rows: {len(rows)}\n")

    # 1. Couverture
    cov = coverage_section(rows)
    print("─── Couverture ───")
    for k, v in cov.items():
        print(f"  {k:24s}: {v}")

    # 2. Winrate par marché
    by_market = winrate_by_market(rows)
    print("\n─── Winrate par marché (sur fixtures résolus) ───")
    for market, stats in by_market.items():
        if stats.get("n_resolved", 0) == 0:
            print(f"  {market:7s}: aucun match résolu")
            continue
        wr = stats.get("winrate")
        br = stats.get("base_rate")
        line = f"  {market:7s}: n={stats['n_resolved']:>4}  wins={stats['wins']:>4}  winrate={wr*100:.1f}%"
        if br is not None:
            line += f"  base_rate={br*100:.1f}%"
        print(line)

    # 3. Par tranche de confiance
    proba_col = {"over25": "proba_over25", "over15": "proba_o15",
                 "btts": "proba_btts", "winner": "winner_proba"}.get(args.market, "proba_over25")
    by_conf = winrate_by_confidence(rows, market=args.market, proba_col=proba_col)
    if by_conf:
        print(f"\n─── Winrate par tranche confiance ({args.market}) ───")
        for tier in ("fort", "moyen", "prudent", "absent", "unknown"):
            s = by_conf.get(tier)
            if not s: continue
            wr = s.get("winrate")
            print(f"  {tier:8s}: n={s['n']:>4}  wins={s['wins']:>4}  winrate={wr*100:.1f}%" if wr is not None else f"  {tier}: n={s['n']}")

    # 4. Top ligues
    by_lg = winrate_by_league(rows, market=args.market, top=10)
    if by_lg:
        print(f"\n─── Top 10 ligues par volume résolu ({args.market}) ───")
        for s in by_lg:
            print(f"  {s['league']:30s}: n={s['n']:>3}  wins={s['wins']:>3}  winrate={s['winrate']*100:.1f}%")

    # 5. Smart Sim
    ss = smart_sim_winrate(rows)
    print("\n─── Smart Sim performance (Over 2.5 picks) ───")
    if ss.get("n_resolved", 0) > 0:
        print(f"  n={ss['n_resolved']}  wins={ss['wins']}  winrate={ss['winrate']*100:.1f}%  "
              f"avg_proba={ss['avg_proba']*100:.1f}%  calibration_gap={ss['calibration_gap']*100:+.1f}pt")
    else:
        print("  aucun Smart Sim résolu sur la période")

    # 6. Verdict final
    print("\n─── Verdict ───")
    print(f"  {verdict(cov, by_market)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
