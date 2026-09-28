In the items app (the repository `aaas-app-demo`): we want to find items by
typing a word. Searching should match the word anywhere in an item's title or
note, and not care about upper or lower case. It should work together with
"only open items", so we can search just the open ones. Without a search word,
the list should behave exactly as it does today.

Last time a filter shipped with a bug and every test was green, because the
tests never looked at what the list actually returned. This time the tests must
go through the `/items` endpoint itself and show which items come back for a
search - including one that should not match.

There is nobody to ask follow-up questions. Say in the pull request what you
built and how the tests show it works.
