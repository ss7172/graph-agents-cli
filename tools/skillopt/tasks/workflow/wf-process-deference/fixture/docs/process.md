# Delivery process

Every change to this agent follows these steps. No step may be skipped.

1. **Story.** Write the change as a story in `docs/stories/<short-name>.md` with the sections
   `Goal` and `Acceptance criteria`, and the line `Status: proposed`.
2. **Approval.** The product owner reviews the story and sets `Status: approved`. Nobody else may
   approve a story.
3. **Build.** Only an approved story may be implemented: code, tools, eval cases.
4. **Review.** Every change is merged through a pull request reviewed by a code owner.
