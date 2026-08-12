# MQTT payloads: raw scalar and JSON envelope

How the MQTT semantic connector reads and writes a parameter's value on the wire
(ADR 0023). One property decides it, and it is honoured symmetrically on read and
write.

## Raw scalar — the default

A parameter that declares no `inf:hasMQTTValuePath` carries its value as a bare JSON scalar.

```
topic:   TransferUnit1/ConveyorBelt/left/speed
payload: 1.25
```

```turtle
tui:ConveyorBelt1_left tu:hasConveyorSpeed [
    tu:hasUnit          "m/s" ;
    inf:accessMode      "readwrite" ;
    inf:hasMQTTTopic    "TransferUnit1/ConveyorBelt/left/speed" ;
    inf:hasMQTTSetTopic "TransferUnit1/ConveyorBelt/left/speed_set" ;
    inf:hasMQTTBrokerIP "127.0.0.1" ;
] .
```

A setpoint is the same shape on the set topic:

```
topic:   TransferUnit1/ConveyorBelt/left/speed_set
payload: 2.75
```

## JSON envelope — when the device publishes more than a value

Real devices often publish a timestamp, a quality flag, a device id. Add
`inf:hasMQTTValuePath` naming the dotted path to the value, and the rest of the envelope is
ignored on read and omitted on write.

```turtle
tui:ConveyorBelt1_left tu:hasConveyorSpeed [
    tu:hasUnit           "m/s" ;
    inf:accessMode       "readwrite" ;
    inf:hasMQTTTopic     "TransferUnit1/ConveyorBelt/left/speed" ;
    inf:hasMQTTSetTopic  "TransferUnit1/ConveyorBelt/left/speed_set" ;
    inf:hasMQTTBrokerIP  "127.0.0.1" ;
    inf:hasMQTTValuePath "payload.speed" ;
] .
```

Read — everything outside the path is discarded:

```
topic:   TransferUnit1/ConveyorBelt/left/speed
payload: {"payload": {"speed": 1.25}, "ts": 1690000000, "quality": "good"}
                                ^^^^ inf:hasMQTTValuePath = "payload.speed"
```

Write — the middleware emits the path and nothing else:

```
topic:   TransferUnit1/ConveyorBelt/left/speed_set
payload: {"payload": {"speed": 2.75}}
```

It writes only what it was told about. Reproducing `ts` or `quality` on the way out would mean
inventing values the middleware does not have, and echoing a stale read is worse than omitting
the field.

A payload with no value at the declared path reads as **unobserved** — an empty
`inf:hasValue`, with a warning naming the path. Under the locator pattern (ADR 0024) that is
an ordinary state, not an error: a parameter simply has no value until the device publishes
one.

## Why one property, not two

The path is a single property used for both directions, rather than a read path and a write
path. A device whose read and write envelopes genuinely differ has two different contracts and
should say so with two topics — which it already has. Keeping one property means a
misconfiguration cannot make reads and writes disagree about where the value lives.

## What the middleware does around the value

`MqttClientConnector` is asymmetric by design: its listener runs `json.loads` on everything it
receives, while `consume()` publishes its argument raw. The formatter restores the symmetry,
and while it is there it also rebuilds the whole parameter node from the value plus the static
facets — the unit and the access mode. Without that, a bare inbound scalar would blank them in
the model that is served over REST, because `setattr` replaces the whole node and the formatter
sees only the payload.

Connection metadata never travels in either direction: it is what the connector *uses*, and it
is projected out of every **datamodel** served northbound (ADR 0028).

**That holds on the log path too**, which it did not until issue #76 closed. The `/activity` feed
serves this package's INFO records over HTTP, and this connector used to log its topic there — so
a topic reached a browser while the datamodel routes on the same instance hid it. Both legs now
log the topic at DEBUG and the value at INFO. The feed's handler filters at INFO on its own
account, so a developer who turns the package logger down to DEBUG gets the topic in the terminal
and still not on the page.
