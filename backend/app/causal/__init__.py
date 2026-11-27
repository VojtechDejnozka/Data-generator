"""Causal & statistical core of Story2Data.

A generic, declarative structural causal model (SCM) engine plus the layers around it:

* ``contracts``      – typed, versioned contracts (SCMSpec, corruption / difficulty / evaluation configs)
* ``results``        – result contracts (validation report, detective result, evaluation, optimisation)
* ``expressions``    – restricted, vectorised expression interpreter (no eval/exec)
* ``distributions``  – explicit parametrisations, inverse-CDF sampling with common random numbers
* ``mechanisms``     – extensible mechanism registry (parameter schema, generation, description)
* ``graph``          – causal graph, within-step acyclicity, execution phases, latent projection, CPDAG
* ``engine``         – clean world generation over relational grains and time steps
* ``validation``     – spec / relational / mechanistic / statistical validation layers
* ``ground_truth``   – ground-truth manifest, realised story facts, interventional effects
* ``corruption``     – observation & corruption engine with a manifest
* ``detective``      – blind analyst (tools sandbox, statistical reference agent, LLM agent)
* ``evaluation``     – graph / story / effect / evidence metrics, recovery score and difficulty
* ``difficulty``     – manual evaluation and automatic difficulty optimisation
* ``editing``        – SCM edit operations with validation and invalidation info
* ``planner``        – LLM proposal of an SCM from a natural-language story
* ``service``        – integration with the existing run storage / job manager
"""
