# Studio Console — Operator Responsibilities

Console is the operator tool for provisioning, configuring, backing up,
restoring, upgrading, and managing Studio deployments. The LICENSE defines
what you may and may not do with Console. This document summarizes the
practical responsibilities that come with operating it. These notes are
informational and do not modify the LICENSE; where they conflict, the LICENSE
governs.

## What Console does and does not grant

Console is a separate artifact from the Studio platform and from Studio
Marketplace packages. Installing or using Console does not grant any right in
Studio or in marketplace content beyond what the Studio Use License and the
Marketplace Terms of Service independently permit. Console manages a
deployment; it does not license the software or content that deployment runs.

## Permitted operation

You may use Console to run and manage your own Studio deployments, and to
install, configure, and manage deployments for individual clients as part of
bespoke service engagements. Ongoing management of a client's deployment
(monitoring, upgrades, backups, support) is permitted.

## The provisioning boundary

The line is the same one the Studio Use License draws — operating versus
distributing:

- **Permitted:** using Console to stand up and manage deployments, one at a
  time, as part of delivering a solution or service.
- **Not permitted:** using Console (or a modified or reimplemented version of
  it) as the engine of a service whose primary function is provisioning,
  hosting, or vending Studio instances to third parties, whether offered
  directly, wrapped, or automated.

Managing a deployment for a client is a service. Offering "Studio instances
on demand" to others is a distribution business, and is not permitted.

## Operator obligations

As the party running Console and the deployments it manages, you are
responsible for:

- Compliance with all applicable laws in your jurisdiction and your
  customers' jurisdictions.
- Securing the infrastructure Console operates on, including credentials,
  tokens, backups, and any secrets Console reads or writes.
- The terms of any third-party services, hosting providers, or model
  providers used by the deployments you manage, including their costs,
  usage limits, and data-handling requirements.
- Any modifications you make to Console and their effects on the deployments
  you manage.

## No warranty

Console is provided "as is," without warranty of any kind. See the LICENSE
for the full disclaimer. Backup, restore, upgrade, and provisioning
operations carry inherent risk; you are responsible for verifying backups and
testing changes before applying them to production deployments.
