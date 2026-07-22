# grill

*An idea. Sharing it early because I want to know if it's stupid.*

---

## The thing I noticed

I read about something. I nod. It makes sense. I close the tab.

Two weeks later someone asks me about it and what comes out of my mouth is a shape — the
right words in roughly the right order, with nothing behind them. I knew *of* the thing. I
didn't know it.

From the inside, those two states are identical. That's the whole problem. There is no
internal signal that distinguishes "I understand this" from "I have watched this be
explained." They feel exactly the same, which means you cannot catch it by being honest
with yourself, or by being smart, or by trying harder. The feeling of understanding is not
evidence of understanding.

This has always been true. Something recent made it much worse.

## What agents did to this

I write a lot of code with Claude now. Most of us do.

Here's what that looks like: I describe a problem. An agent produces something good. I read
it, it makes sense, I ship it. Repeat that across a few PRs and something strange happens —
I now have a *pattern in production* that I could not defend in an interview. I've watched
it work three times. I've never once had to know why.

That used to be impossible. Getting code to work *was* the forcing function; you couldn't
ship what you didn't understand, because it wouldn't run. That forcing function is gone.
Shipping and understanding have come apart, and nothing has replaced the gap.

So you get people — I am one — accumulating enormous surface area and much less depth than
their commit history implies. Not because anyone's lazy. Because the thing that used to
force the issue quietly stopped.

## The idea

**One question, when you finish coding.**

You close a Claude session. Before it's gone, it asks you one thing about something you
actually engaged with — usually whatever you stopped to ask about. Twenty seconds. Answer
it, or press enter and it leaves.

If the answer is hollow, it says so, quotes you back to yourself, tells you where to look,
and offers to keep going. If it's solid, it says nothing and gets out of the way. And some
days it says nothing at all — if you spent the session renaming variables and fixing CI,
there's nothing to be hollow about, and asking anyway would just be nagging.

```
You added a retry wrapper around the payment call today.
It retries on timeout. What happens if the first attempt
actually succeeded and the response got lost on the way back?

before you answer — how sure are you?   [ 95% · 70% · 40% ]

> 95%. the retry would go through again, but stripe handles that?

Does it? What in your wrapper tells Stripe these are
the same charge?

> ...
```

Note what that question isn't. It isn't "what is a retry policy." You can answer that one
from memory, and answering it proves nothing. It's about the code *I shipped this
afternoon*, and there is no way to answer it except by actually understanding what I
shipped.

That's the whole product. Not a course. Not a tutorial. Not another AI explaining
idempotency to you — you can get that anywhere in fifteen seconds and it's worth about
that much.

The scarce thing isn't explanation. It's **calibration** — the gap between how well you
think you understand something and how well you actually do. That's what the "how sure are
you?" tap reaches for: a wrong answer you flagged as a guess is fine, but a wrong answer
you were sure of is the whole point — you, catching the thing you're most confidently wrong
about before it hardens into production knowledge, and then knowing which fifteen seconds of
explanation are *yours*. I'm not certain the rating survives real use; maybe everyone just
taps 95% because it's fastest. So it's an experiment, not a pillar — and its payoff isn't in
the moment anyway.

It's a week later. The tool comes back — and this is the half that matters most, not a
footnote, because you *do* forget and you don't notice that either. So it asks again: not
the same question, but whether last Tuesday's hollow spot filled in, and whether the thing
you were 95% sure of held up. That second visit is where a one-off quiz becomes evidence
that understanding actually formed, and the only place the confidence tap earns its keep.

## Why it might work

**It can't be fooled by fluency, and I can.** Left alone, I measure my own understanding by
how smoothly the words come out. That metric is worthless — fluency is the *symptom* of the
disease. An interrogator that follows up doesn't care how good your first sentence was.

**It only needs one honest failure to pay for itself.** Getting told "you've used this
pattern three times and can't explain why it works" once is worth more than a month of
courses, because now you know where to actually look.

**There's nothing to remember.** It fires when a session ends, on a topic it pulled out of
the session itself. I never have to decide to use it, or open anything, or feed it. The
step where a human has to volunteer input on a Tuesday is where every tool built on good
intentions goes to die — so there isn't one.

That last one is a change of mind. The first version of this was a little web app I'd visit
when I felt like it. I wrote "the user will not show up daily" into my own design doc as a
constraint, designed carefully around it, and somehow didn't notice I'd just described a
product with no users. The portal is gone. It's a plugin now.

## Why it might not

**The questions might just be bad, and that's the whole ballgame.** A mediocre judge is
annoying; you shrug it off. A mediocre *question* makes the entire interaction feel fake,
and fake is unrecoverable — "what is a retry policy" tells you instantly this thing has
nothing to teach you, and you never open it again. So the bar isn't "a relevant question."
It's a question that makes an experienced engineer stop for thirty seconds and go *"…huh"* —
every session, from their own code, reliably. I don't know yet that an LLM can clear that
bar. If it can't, none of the rest of this matters, which is why it's the first thing I'm
validating, ahead of the judge and everything else.

**The judge has two ways to fail, and they're opposite.** Ask an LLM to assess your
explanation and it will tell you you're doing great — accept a vague answer, quietly fill
in what you left out, congratulate you. LLMs are cowards. That's the failure I worried
about first: if I can't fix it, this whole thing is an expensive machine for making me feel
smart, which is the precise opposite of the point.

But there's a worse one, and I underrated it. The judge misreads the code, invents a bug I
didn't write, and tells me confidently I don't understand something I do. Flattery just
wastes my time. A confident false accusation about *my own code, in front of me* gets the
plugin disabled forever — and it should, because trust is the whole product and it's
decided in the first ten interactions. And it's judging on almost no evidence — one
question, not five — which makes a confident misread *more* likely, not less.

So the design leans the whole way to one side: it *asks*, it never *accuses*. A question
can't be a false positive the way a verdict can. And it makes you commit a confidence
before you answer, so when a gap shows up it's measured against *your own* number — "you
said 95%" is not an argument you can have with a computer. The coward I think is fixable
structurally: separate the interrogator from the judge, decide what a real answer contains
*before* hearing yours, force it to quote you back. The zealot is fixable by never issuing
a verdict it isn't forced to defend with your own words, and by making "your premise is
wrong, that's not what my code does" a first-class answer. If I catch myself arguing with
it about my own code, that's not the tool being tough — that's the bug that kills it, and
after the questions themselves it's the thing I most need to get right.

**Maybe nobody cares.** Claude wrote it, tests passed, PR merged, salary arrived — why
spend another twenty seconds to find out you're hollow? I used to answer "because
eventually someone asks you in an interview," and that's not enough: a cost today against a
benefit six months out, maybe. I could claim it prevents outages or catches rot before it
ships. I don't know that, and dressing up a guess as a value proposition would make this
exactly the document I'm trying not to write. What I know is that *I* want to know. If very
few people are like me, this is a good tool for a small number of people rather than a
product — fine, but I'd rather learn that on purpose than by accident. I can write a test
for a cowardly judge. There's no test for whether anyone wants this, which is why it's the
risk that actually scares me.

**It spends your tokens on a question you might not answer.** The question gets written when
the session ends, on whatever model you already had selected — not a key you configure, just
the one you're already using. So the twenty seconds isn't quite the whole price.

I assumed this was a real objection and it mostly isn't. I measured it: across 107 of my own
sessions, 290MB of transcript contains 80KB of things I actually typed — everything else is
tool output and file dumps. Four in ten sessions contain no human input at all, so there's
nothing to ask about and nothing to spend. What costs something is the code, because a
question about *your* retry wrapper means reading it: around 36KB for a median session. Ten
thousand tokens, once, on a session you've already finished. I'd rather say that plainly than
keep a caveat I'd written before checking whether it was true.

**And it might just be nagging** — a sophisticated way of making you feel bad about things
you'd decided not to care about. It also has to survive the opposite: the reflex to swat it
away. If enter dismisses it in half a second, that reflex wins and the tool trains its own
banner blindness. The fix isn't to make skipping cost something — guilt kills these tools —
it's that the question has to earn the half-second by being *sharp*, not *alarming*, and
stay silent on the days there's nothing worth asking. Skipping stays free and always valid;
skip three in a row and it goes quiet for a week without a word. The tool is built to notice
it isn't wanted and leave. That's how it behaves when you don't want it — which is a
different question from whether you'll want it on day one, and that one still takes being
willing to be wrong in front of a computer, repeatedly, on purpose. Nobody has built a
successful product around voluntarily feeling stupid.

## Where it's at

Design's done. Nothing's built.

Building the interrogator first, and pointing it at real transcripts rather than topics I
type in by hand — because the way this dies is misreading code I actually wrote, and a
hand-fed topic is the one case where that can't happen. The session hook is plumbing. The
resurfacing isn't — it's where calibration turns from a single quiz into evidence that
understanding did or didn't form, so it's the second thing I build, not an afterthought.

Either I find out that being questioned changes what people actually understand, or I find
out that nobody wants to know what they don't. Both answers are worth having in a week
rather than a month.

---

**What I want to know from you:**

- Do you recognize this? The pattern-in-production-you-can't-defend thing?
- Would you use it, honestly — or does "voluntarily get interrogated about your gaps"
  sound like something you'd try twice and quietly stop?
- One question at the end of every coding session: useful, or the kind of thing you'd
  disable in week two? I've tried to make it cheap enough to survive, but I'm the wrong
  person to judge whether it is.
- Does something like this already exist and I've missed it?
