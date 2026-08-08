import { describe, expect, it } from 'vitest'
import type { JobRecord } from './types'
import { jobsForRun } from './App'

const oldRunJob = { id: 'JOB-OLD', run_id: 'RUN-OLD' } as JobRecord
const newRunJob = { id: 'JOB-NEW', run_id: 'RUN-NEW' } as JobRecord

describe('run-scoped durable job state', () => {
  it('never renders a previous run job while a new run is still loading', () => {
    const cache = { 'RUN-OLD': [oldRunJob] }

    expect(jobsForRun('RUN-NEW', cache)).toEqual([])
  })

  it('keeps out-of-order responses isolated under their own run id', () => {
    const cache = { 'RUN-NEW': [newRunJob], 'RUN-OLD': [oldRunJob] }

    expect(jobsForRun('RUN-NEW', cache)).toEqual([newRunJob])
  })
})
