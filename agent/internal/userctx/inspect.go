package userctx

import (
	"encoding/json"
)

// InspectedTurn explicitly marks omitted payloads. The durable Turn is never
// modified and no placeholder is presented as a recorded model message.
type InspectedTurn struct {
	Turn
	InspectionTruncated  bool   `json:"inspection_truncated,omitempty"`
	OriginalMessageBytes int    `json:"original_message_bytes,omitempty"`
	InspectionNote       string `json:"inspection_note,omitempty"`
}

// RecentArchive reads the durable archive, distinct from the prompt cache.
func (s *Store) RecentArchive(limit int) ([]InspectedTurn, error) {
	if limit < 1 || limit > 50 {
		limit = 50
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	rows, err := s.db.Query(`SELECT seq,input_id,kind,t,
		CASE WHEN length(CAST(messages AS BLOB)) <= ? THEN messages ELSE '' END,
		length(CAST(messages AS BLOB)) FROM turns ORDER BY seq DESC LIMIT ?`, 256<<10, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []InspectedTurn{}
	remaining := 384 << 10
	for rows.Next() {
		var turn InspectedTurn
		var raw string
		if err := rows.Scan(&turn.Seq, &turn.InputID, &turn.Kind, &turn.T, &raw, &turn.OriginalMessageBytes); err != nil {
			return nil, err
		}
		if raw == "" || len(raw) > remaining {
			turn.InspectionTruncated = true
			turn.InspectionNote = "inspection_byte_limit; original preserved in SQLite"
			turn.Messages = nil
		} else if err := json.Unmarshal([]byte(raw), &turn.Messages); err != nil {
			return nil, err
		} else {
			remaining -= len(raw)
		}
		out = append(out, turn)
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, rows.Err()
}

// ClearHistory atomically clears history/summary while preserving facts/rules.
func (s *Store) ClearHistory() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.Exec("DELETE FROM turns"); err != nil {
		return err
	}
	if _, err = tx.Exec("DELETE FROM summary"); err != nil {
		return err
	}
	if err = tx.Commit(); err != nil {
		return err
	}
	s.turns = nil
	s.turnCount = 0
	s.summary = ""
	s.summaryUpto = 0
	return nil
}
