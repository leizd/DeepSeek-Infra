# Implementation Plan: 4.6.0 Autonomous Recovery Placement & Scale-Safe Storage Control

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Overview

Upgrade the 4.5.9 Storage Control Plane into an Autonomous SLO Controller that
remains correctness-safe at 500+ recovery points and 20k+ object references.
Wire freeze: object-set-v1, Receipt v4, Commit v4, FastCDC v3, Projection,
randomized Age.

## Gate A — Scale-Safe Correctness Closure (first)

1. Canonical physical ciphertext identity (aliases must not inflate usage)
2. GC correctness without bounded live-ref materialization (no 20k fail-open)
3. DR Readiness zero remote I/O / zero side effects (capacity projection only)
4. Recovery chain closure without history caps; missing parent fails closed

## Later gates

- B Read Model purity (instrumentation)
- C Indexed lineage graph
- D RecoveryChainMigrationJob
- E Autonomous SLO Controller
- F Truly sharded maintenance
- G Planner-mandatory Evidence
