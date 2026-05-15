-- ════════════════════════════════════════════════════════════════════
-- Migration : ajout colonnes résultats (Phase 1 — sync-results)
-- Date     : 2026-05-15
-- Objectif : permettre la synchronisation post-match des résultats
--            réels dans bet_history, calculer win/loss par marché,
--            mesurer le ROI théorique et la précision modèle.
--
-- Réversible  : DROP COLUMN possible sans perte des autres données.
-- Idempotent  : utilise ADD COLUMN IF NOT EXISTS partout.
-- Non bloquant: ALTER TABLE ADD COLUMN avec defaults NULL → instantané
--               sur PostgreSQL ≥ 11, pas de rewrite de table.
--
-- À exécuter MANUELLEMENT côté Supabase (SQL editor ou supabase CLI)
-- après validation. Aucune exécution automatique.
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE bet_history
  -- Résultats vérifiés (calculés post-match par sync-results)
  ADD COLUMN IF NOT EXISTS result_over25_won  boolean,     -- (hg + ag) > 2
  ADD COLUMN IF NOT EXISTS result_over15_won  boolean,     -- (hg + ag) > 1
  ADD COLUMN IF NOT EXISTS result_btts_won    boolean,     -- hg > 0 AND ag > 0
  ADD COLUMN IF NOT EXISTS result_winner_won  boolean,     -- predicted == actual

  -- Métriques de match calculées (pratique pour stats agrégées)
  ADD COLUMN IF NOT EXISTS total_goals        integer,     -- hg + ag
  ADD COLUMN IF NOT EXISTS actual_winner      text,        -- "home" / "draw" / "away"

  -- Métadonnées de synchronisation
  ADD COLUMN IF NOT EXISTS resolved_at        timestamptz; -- timestamp du sync

-- Index léger pour cibler rapidement les fixtures à synchroniser
-- (utile pour le worker sync-results qui SELECT WHERE resolved_at IS NULL).
CREATE INDEX IF NOT EXISTS idx_bet_history_resolved_at
  ON bet_history (resolved_at)
  WHERE resolved_at IS NULL;

-- Index sur (date, match_status) pour les vues monitoring quotidien
CREATE INDEX IF NOT EXISTS idx_bet_history_date_status
  ON bet_history (date, match_status);

-- Commentaires colonnes (documentation Supabase)
COMMENT ON COLUMN bet_history.result_over25_won IS
  'TRUE si total buts > 2 (résultat vérifié post-match). NULL tant que match non terminé.';
COMMENT ON COLUMN bet_history.result_over15_won IS
  'TRUE si total buts > 1.';
COMMENT ON COLUMN bet_history.result_btts_won IS
  'TRUE si les deux équipes ont marqué.';
COMMENT ON COLUMN bet_history.result_winner_won IS
  'TRUE si winner prédit (home/draw/away) == actual_winner.';
COMMENT ON COLUMN bet_history.total_goals IS
  'Total buts du match (hg + ag).';
COMMENT ON COLUMN bet_history.actual_winner IS
  'Résultat final 1X2 : home / draw / away.';
COMMENT ON COLUMN bet_history.resolved_at IS
  'Timestamp de la dernière synchronisation des résultats par le worker sync-results.';
