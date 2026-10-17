-- Sample problem so a fresh stack can be tried immediately.
INSERT INTO problems (id, title, time_limit_s) VALUES ('sum-two', 'Sum of two integers', 10)
ON CONFLICT (id) DO NOTHING;

INSERT INTO test_cases (problem_id, ordinal, stdin, expected_stdout) VALUES
    ('sum-two', 1, E'1 2\n', E'3\n'),
    ('sum-two', 2, E'-5 5\n', E'0\n'),
    ('sum-two', 3, E'1000000 2000000\n', E'3000000\n')
ON CONFLICT (problem_id, ordinal) DO NOTHING;
