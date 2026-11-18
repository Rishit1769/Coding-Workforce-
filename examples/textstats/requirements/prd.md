# textstats - Product Requirements

## Overview
textstats is a tiny Python library for text statistics.

## Functional Requirements
- FR-1: `textstats.counting.word_count(text)` must return the number of whitespace-separated words.
  - an empty or whitespace-only string returns 0
- FR-2: `textstats.counting.char_count(text, include_spaces=True)` must return the number of characters; when include_spaces is False, whitespace characters are not counted.
- FR-3: `textstats.ranking.top_words(text, n)` must return the n most frequent lowercase words as (word, count) tuples ordered by count descending then word ascending.
  - n must be a positive integer; otherwise ValueError is raised
