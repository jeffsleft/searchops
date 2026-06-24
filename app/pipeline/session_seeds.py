"""Interview session types and seed content — copied verbatim from prep.jsx TYPE_SEEDS."""

SESSION_TYPES = {
    'recruiter':    {'label': 'Recruiter screen',    'short': 'Recruiter', 'hint': 'Fit, comp, logistics, motivation',           'hue': 200},
    'hm':           {'label': 'Hiring manager',      'short': 'HM',        'hint': 'Scope, success criteria, alignment',         'hue': 170},
    'hm_followup':  {'label': 'HM follow-up',        'short': 'HM #2',     'hint': 'Open threads, deeper dive',                  'hue': 170},
    'peer':         {'label': 'Peer / cross-fn',     'short': 'Peer',      'hint': 'Collaboration, cross-functional fit',        'hue': 240},
    'panel':        {'label': 'Panel / loop',        'short': 'Panel',     'hint': 'Multiple interviewers; route Qs to personas','hue': 145},
    'final':        {'label': 'Final / executive',   'short': 'Exec',      'hint': 'Vision, trust, mutual commitment',           'hue': 80},
    'presentation': {'label': 'Presentation / case', 'short': 'Present',   'hint': 'Show the work; anticipate critique',         'hue': 45},
    'custom':       {'label': 'Custom session',      'short': 'Custom',    'hint': 'Free-form session',                          'hue': 60},
}

STAGE_TO_DEFAULT_TYPE = {
    'researching':  'recruiter',
    'outreach':     'recruiter',
    'recruiter':    'recruiter',
    'hm_interview': 'hm',
    'panel':        'panel',
    'final_offer':  'final',
}

TYPE_SEEDS = {
    'recruiter': {
        'questions_to_ask': [
            ('What does the full interview process look like and what is the timeline?', 'High', 'Any'),
            ('What is the comp range — base, bonus, equity, and how is equity structured?', 'High', 'Any'),
            ('What does the team I would be joining look like? Reporting line?', 'High', 'Any'),
            ('Why is this role open?', 'High', 'Any'),
            ('What would make a candidate stand out for the next round?', 'Medium', 'Any'),
        ],
        'questions_they_ask': [
            'Walk me through your background.',
            'Why this company and why now?',
            'What is your earliest start date?',
            'What is your comp expectation?',
            'Are you actively interviewing elsewhere? Where in the process?',
        ],
        'red_flags': [
            'Process described as "fast" or "accelerated" — possible pressure tactic',
            'Vague compensation range or refusal to share',
            'Cannot articulate why the role is open',
        ],
    },
    'hm': {
        'questions_to_ask': [
            ('What does success look like in this role at 30/60/90 days and 12 months?', 'High', 'HM'),
            ('Describe the most important problem you need this role to solve.', 'High', 'HM'),
            ('Where does this function sit organizationally? Who does it report to?', 'High', 'HM'),
            ('What is the budget — headcount and tooling — for this function in year one?', 'High', 'HM'),
            ('What has been tried before in this space, and why didn\'t it stick?', 'Medium', 'HM'),
        ],
        'questions_they_ask': [
            'Tell me about the last time you built something from zero.',
            'What is your operating philosophy?',
            'How do you decide what NOT to work on?',
            'Walk me through a project where you had to push back on leadership.',
        ],
        'red_flags': [
            'Cannot articulate a specific success metric',
            'Wants the role but has no exec air cover',
            'Inherited tech debt the HM won\'t acknowledge',
        ],
    },
    'hm_followup': {
        'questions_to_ask': [
            ('What concerns from our last conversation would you like to address today?', 'High', 'HM'),
            ('Who else have I spoken with that you\'ve gotten feedback from?', 'Medium', 'HM'),
            ('What\'s the gap between today and where you need to be in 12 months?', 'High', 'HM'),
        ],
        'questions_they_ask': [
            'I\'ve had time to think about your background — tell me more about [specifics].',
            'What did you take away from your conversations with the team?',
        ],
        'red_flags': [],
    },
    'peer': {
        'questions_to_ask': [
            ('How do cross-functional decisions get made here?', 'High', 'Peer'),
            ('What\'s the relationship like between your team and the function I\'d be running?', 'High', 'Peer'),
            ('What\'s the biggest blocker you face that this role could help with?', 'High', 'Peer'),
            ('Where do most miscommunications happen between functions today?', 'Medium', 'Peer'),
        ],
        'questions_they_ask': [
            'Tell me about a time you had to influence without authority.',
            'How do you handle disagreement with a peer?',
            'Walk me through a cross-functional project you led.',
        ],
        'red_flags': [
            'Peer is visibly frustrated about a topic but won\'t name it',
            'Avoids saying anything specific about leadership',
        ],
    },
    'panel': {
        'questions_to_ask': [
            ('For each of you — what is the most important thing your function needs from this role in year one?', 'High', 'Panel'),
            ('Where do you see your function and mine collaborating most?', 'High', 'Panel'),
            ('What is one thing you wish someone in this role would stop doing?', 'Medium', 'Panel'),
        ],
        'questions_they_ask': [],
        'red_flags': [
            'Panel members give wildly different answers to the same question',
            'Body language suggests internal disagreement',
        ],
    },
    'final': {
        'questions_to_ask': [
            ('What is the vision in 3 years? What has to be true for that to happen?', 'High', 'CEO/Exec'),
            ('Where do you personally see the biggest risk to that vision?', 'High', 'CEO/Exec'),
            ('How do you make decisions when the team is split?', 'Medium', 'CEO/Exec'),
            ('What would make you regret making this hire 12 months from now?', 'High', 'CEO/Exec'),
        ],
        'questions_they_ask': [
            'Why should I bet on you for this role over the other finalists?',
            'What is your decision criteria for accepting an offer?',
            'What do you want from your next role that you didn\'t get from your last?',
        ],
        'red_flags': [
            'Cannot articulate a specific vision',
            'Defensive when asked about company risk',
            'Mentions "we work hard here" as the culture point',
        ],
    },
    'presentation': {
        'questions_to_ask': [
            ('Who is in the room and what do they care about most?', 'High', 'HM'),
            ('How interactive should this be — slides through, or open dialogue?', 'High', 'HM'),
            ('Are there specific themes you want me to cover or avoid?', 'Medium', 'HM'),
            ('What would make this a clear win in your eyes?', 'High', 'HM'),
        ],
        'questions_they_ask': [
            'Walk us through your approach.',
            'What assumptions are you making? What would change if any were wrong?',
            'What would you do differently with twice the budget? Half?',
            'What would you do in the first 30 days based on this?',
        ],
        'red_flags': [
            'Audience disengaging during a specific section',
            'Repeated questions about the same assumption — sign of skepticism',
        ],
    },
    'custom': {
        'questions_to_ask': [],
        'questions_they_ask': [],
        'red_flags': [],
    },
}


def seed_session_content(conn, session_id: int, type_id: str):
    """Insert the type-specific seed rows into the child tables. Call once on session creation."""
    seed = TYPE_SEEDS.get(type_id, TYPE_SEEDS['custom'])

    for i, (text, prio, persona) in enumerate(seed.get('questions_to_ask', [])):
        conn.execute(
            "INSERT INTO session_questions_to_ask (session_id, text, priority, persona, position) VALUES (?, ?, ?, ?, ?)",
            (session_id, text, prio, persona, i),
        )

    for i, prompt in enumerate(seed.get('questions_they_ask', [])):
        conn.execute(
            "INSERT INTO session_questions_they_ask (session_id, prompt, position) VALUES (?, ?, ?)",
            (session_id, prompt, i),
        )

    for i, text in enumerate(seed.get('red_flags', [])):
        conn.execute(
            "INSERT INTO session_red_flags (session_id, text, position) VALUES (?, ?, ?)",
            (session_id, text, i),
        )


def seed_pinned_anchors(conn, session_id: int):
    """Pin the top 3 anchor stories for a new session as a starting suggestion."""
    anchors = conn.execute(
        "SELECT id FROM anchor_stories ORDER BY strongest DESC, id LIMIT 3"
    ).fetchall()
    for a in anchors:
        conn.execute(
            "INSERT OR IGNORE INTO session_pinned_anchors (session_id, anchor_id) VALUES (?, ?)",
            (session_id, a['id']),
        )
