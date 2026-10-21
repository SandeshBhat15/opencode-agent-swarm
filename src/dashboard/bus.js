/**
 * Bounded event bus.
 *
 * Two hard requirements, both structural rather than advisory:
 *  1. a subscriber can never grow the queue without limit — the oldest event is
 *     dropped and counted, and the client is told it lagged;
 *  2. a subscriber can never fail the publisher — every dispatch is wrapped and
 *     an exception only costs that subscriber its subscription.
 */

import { makeEvent } from "./events.js";

export const EVENT_LIMIT = 200;
export const QUEUE_LIMIT = 64;
export const MAX_SUBSCRIBERS = 8;

export function createEventBus({
  limit = EVENT_LIMIT,
  queueLimit = QUEUE_LIMIT,
  maxSubscribers = MAX_SUBSCRIBERS,
  now = () => Date.now(),
} = {}) {
  /** @type {object[]} */
  const events = [];
  /** @type {Set<{id:number, fn:Function, queue:object[], dropped:number, failed:number, scheduled:boolean}>} */
  const subscribers = new Set();
  let seq = 0;
  let nextId = 0;
  let totalPublished = 0;
  let rejectedSubscribers = 0;

  function publish(type, data = {}) {
    const event = makeEvent(type, data, { now, seq: ++seq });
    events.push(event);
    if (events.length > limit) events.splice(0, events.length - limit);
    totalPublished += 1;

    for (const subscriber of subscribers) {
      try {
        if (subscriber.queue.length >= queueLimit) {
          subscriber.queue.shift();
          subscriber.dropped += 1;
        }
        subscriber.queue.push(event);
        if (!subscriber.scheduled) {
          subscriber.scheduled = true;
          // Never dispatch synchronously: a slow consumer must not extend the
          // publisher's call frame.
          queueMicrotask(() => flush(subscriber));
        }
      } catch {
        drop(subscriber);
      }
    }
    return event;
  }

  function flush(subscriber) {
    subscriber.scheduled = false;
    while (subscriber.queue.length) {
      const event = subscriber.queue.shift();
      try {
        subscriber.fn(event);
      } catch {
        subscriber.failed += 1;
        if (subscriber.failed >= 3) {
          drop(subscriber);
          return;
        }
      }
    }
  }

  function drop(subscriber) {
    subscribers.delete(subscriber);
  }

  function subscribe(fn) {
    if (typeof fn !== "function") throw new TypeError("subscribe requires a function");
    if (subscribers.size >= maxSubscribers) {
      rejectedSubscribers += 1;
      return null;
    }
    const subscriber = { id: ++nextId, fn, queue: [], dropped: 0, failed: 0, scheduled: false };
    subscribers.add(subscriber);
    return {
      id: subscriber.id,
      get dropped() {
        return subscriber.dropped;
      },
      unsubscribe() {
        drop(subscriber);
        subscriber.queue.length = 0;
      },
    };
  }

  return {
    publish,
    subscribe,
    /** Newest `count` events, oldest first. */
    tail(count = 50) {
      const size = Math.max(0, Math.min(count, events.length));
      return events.slice(events.length - size);
    },
    snapshot(count = 50) {
      return { seq, events: this.tail(count) };
    },
    stats() {
      return {
        published: totalPublished,
        retained: events.length,
        limit,
        subscribers: subscribers.size,
        rejectedSubscribers,
      };
    },
    /** Removes every subscriber; used by `stop()` and by tests. */
    clear() {
      for (const subscriber of [...subscribers]) drop(subscriber);
      events.length = 0;
    },
    get subscriberCount() {
      return subscribers.size;
    },
    get seq() {
      return seq;
    },
  };
}
