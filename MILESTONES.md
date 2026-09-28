# How milestones are used

This is how my repositories use GitHub milestones. It is written for anyone working on one of them,
human collaborators and coding agents alike. [`milestones`](README.md) is the tool that keeps it
tidy across all of them, but nothing here depends on it.

A repository has two kinds of milestone: **standing buckets**, which say how urgent an issue is and
are never closed, and **release or deadline milestones**, which say when an issue has to be done by
and are closed once everything on them is. Every issue should end up on one or the other.

## Standing buckets

| Bucket         | For                                                                                   |
|----------------|---------------------------------------------------------------------------------------|
| `Critical`     | Must go into the next release, whatever else happens.                                 |
| `Needed soon`  | Not being worked on now, but should be next.                                          |
| `Needed later` | Wanted, but can wait until the soon work is done.                                     |
| `Not urgent`   | Worth doing some day. Nobody is waiting on it.                                        |
| `Upstream`     | Belongs to an upstream data or code source. Finalise it here, then report it there.   |
| `Needs triage` | Seen, but can't be placed yet: needs a reproduction, tests, or an investigation first. |

`Needed soon`, `Needed later` and `Not urgent` are for work that can be put aside for now, and they
run in decreasing urgency. `Critical` is the exception: an issue there has to be fixed before the
next release. `Upstream` is not a level of urgency. It is for issues that are only fully resolved
once someone else fixes their data or code.

`Needs triage` is not a level of urgency either. It is for an issue somebody has looked at and
could not place: not until it has been reproduced, or a question about it answered, or its scope
talked through. That is different from an issue with no milestone, which nobody has looked at
yet (see [below](#issues-with-no-milestone)). Sometimes it just means a person set it without
knowing that no milestone would have said the same thing, and that is fine: triage covers both.
If you put an issue there, say in a comment what has to be found out first, so that the next
person to look knows where to start. Parking an issue is a person's call: a coding agent that
can't place one leaves it without a milestone, as the next section says.

Buckets are undated. A bucket gets a due date only while it is urgent — say, when upstream sources
need contacting by a certain day — and loses it again once the urgent work is done. A bucket still
past its date means exactly that: an urgent thing has been missed.

A bucket is never closed or deleted, even when it is empty. A closed bucket drops out of sight, and
then nothing can be triaged into it.

Buckets are per repository, and a repository can use any of them or none. None of them is
required. What matters is that a bucket means the same thing wherever it exists, so its name alone
says how urgent an issue on it is. `Critical` usually doesn't exist until something is critical, and
is created then. A repository run by someone else may use the same names a little more loosely, and
that is still close enough to read them by.

## Release and deadline milestones

Every other milestone is named for a release (`tool-name v1.2`) or a date (`2026aug22`). Some
deadlines are hard or semi-hard: v1.2 might be due for testing on a given date, or 2026aug22 might
be the last day a build can start and still finish in time for a later deadline. Others are soft.
In a repository whose last release was v1.1, an undated `v1.2` simply means "the next release,
whenever that happens". That still ranks above `Needed soon`.

Release milestones usually have a due date. Most of those dates are soft, so a milestone that is
past its date is normal and not an alarm. A useful nudge is one that notices a milestone really has
become urgent and suggests giving it a date that says so.

A release milestone is closed when its release goes out (see [Releases](#releases)). A deadline
milestone is closed once the work on it is done, whether that happens before its date or after it.
The date passing doesn't close it.

## Releases

Unless a repository documents a different process, such as a GitHub Project, a milestone is how a
release is put together. The issues on it are fixed, the release is built and published as a GitHub
release, and then the milestone is closed. Anything that didn't make it moves on to the next
milestone rather than staying on a closed one.

## Issues with no milestone

Eventually every issue should be on a milestone. An issue gets one when it is filed only if the
right one is clear: it is plainly critical, or it is needed for a particular release. Otherwise it
is fine to leave it without one. Triage (`milestones triage`) works through the issues that have
no milestone, and then the ones on `Needs triage`, so either will be picked up in time. A guessed
milestone is harder to spot than a missing one.

A pull request is work already under way, so it always belongs on a milestone, and it stands in for
the issues it closes: an open issue a pull request closes goes on the same milestone as the pull
request if it has no milestone yet. One that already has a milestone keeps it, since that was
somebody's decision; say so if the two look inconsistent rather than moving it.

## For coding agents

- Set a milestone on an issue when the right one is clear. When it isn't, leave the issue without
  one. Don't put an issue on `Needs triage`, even when you have looked and it plainly needs a
  reproduction or an investigation first: that bucket is for a person's judgement, and an issue
  with no milestone reaches the same triage anyway. Say what you found in a comment instead.
- Never move an issue off `Needs triage`. Somebody parked it there for a reason; if you think you
  know where it belongs, say so in a comment.
- Never close, delete or rename a standing bucket.
- Suggest due-date changes; don't make them. The same goes for closing a release milestone outside
  of a release.
- Read a repository's own contributing notes first. If they describe a different process, such as a
  GitHub Project, follow them over this file.
