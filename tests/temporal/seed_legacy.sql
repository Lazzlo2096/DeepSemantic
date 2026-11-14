INSERT INTO arxiv_imports (id, date_from, date_to, bucket, status, lease_until)
VALUES ('00000000-0000-0000-0000-000000000001', '2026-11-01', '2026-11-01', 'papers', 'running', now() + interval '2 minutes');
INSERT INTO arxiv_papers (arxiv_id, bucket, object_key, title, authors, abstract, submitted_at, downloaded_at, status, lease_until)
VALUES ('2611.99999v1', 'papers', 'legacy.html', 'Legacy', ARRAY['A'], 'Abstract', now(), now(), 'texting', now() + interval '5 minutes');
INSERT INTO paper_summaries (arxiv_id, status, lease_until) VALUES ('2611.99999v1', 'running', now() + interval '2 minutes');
