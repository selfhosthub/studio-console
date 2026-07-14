# Studio Console: Operator Responsibilities

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
Studio Marketplace Package License independently permit. Console manages a
deployment; it does not license the software or content that deployment runs.

## Permitted operation

You may use Console to run and manage your own Studio deployments, and to
install, configure, and manage deployments for individual clients as part of
bespoke service engagements. Ongoing management of a client's deployment
(monitoring, upgrades, backups, support) is permitted.

## The provisioning boundary

The line is the same one the Studio Use License draws, operating versus
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
- The licenses of any model files a deployment downloads or runs. Console
  provisions stacks that fetch model weights on first use; those weights
  carry their own licenses (CreativeML OpenRAIL-M and others), the terms
  vary significantly, and some restrict commercial use. Console distributes
  no model files and grants no rights in them.
- Any modifications you make to Console and their effects on the deployments
  you manage.

## No warranty

Console is provided "as is," without warranty of any kind. See the LICENSE
for the full disclaimer. Backup, restore, upgrade, and provisioning
operations carry inherent risk; you are responsible for verifying backups and
testing changes before applying them to production deployments.

## No professional advice

Nothing in Console's documentation, code, or output constitutes legal, tax,
accounting, or financial advice. Cost figures, provider pricing, and sizing
guidance in the documentation are illustrative, change without notice, and
are set by the providers rather than by us. Consult qualified professionals
regarding your own obligations.

## Limitation of liability

To the maximum extent permitted by applicable law, in no event shall the
authors or copyright holders be liable for any claim, damages, or other
liability arising from or in connection with Console or its use, including:

- Data loss or corruption from backup, restore, upgrade, or reset operations.
- Misconfiguration of DNS, TLS, tunnels, or access policies performed through
  Console, and any exposure resulting from it.
- Compromise of credentials, tokens, or secrets that Console reads or writes.
- Downtime, cost overruns, or account actions imposed by hosting, registry,
  or model providers.
- Regulatory fines, penalties, or claims incurred by operators or their
  end users.

## Indemnification

By using Console, you agree to indemnify and hold harmless the authors and
copyright holders from any claims, damages, losses, or expenses (including
legal fees) arising from your use of Console, your failure to comply with
applicable laws or third-party terms, your relationships with your own
clients or end users, and any modifications you make to Console.

## Jurisdiction

This document shall be governed by and construed in accordance with
applicable laws, without regard to conflict of law principles.
