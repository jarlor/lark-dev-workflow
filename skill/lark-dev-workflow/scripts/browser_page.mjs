/** Fixed page-side readers. Never evaluate expressions obtained from Base content. */
export async function readTable(input) {
  const end = Date.now() + 20000;
  const pause = () => new Promise((resolve) => setTimeout(resolve, 200));
  const encode = (value) => JSON.stringify(value, (_key, item) =>
    item && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
  for (;;) {
    const store = window.bitableStore;
    const sdk = window.commentSDK;
    if (store?.getCurrentUserId && sdk?.serviceToken) {
      const userId = store.getCurrentUserId();
      if (String(userId) !== input.user_id) return { status: 'failed', code: 'ACCOUNT_MISMATCH' };
      if (sdk.serviceToken !== input.base_token) return { status: 'failed', code: 'RESOURCE_MISMATCH' };
      const table = store.modelOperator?.getTableById(input.table_id);
      if (table && ['fieldsToJSON', 'getRecordCount', 'getLoadedRecordCount', 'getRecordIds',
        'getRecordDataById'].every((name) => typeof table[name] === 'function')) {
        const capture = () => {
          const fieldsById = table.fieldsToJSON();
          const ids = table.getRecordIds();
          const total = table.getRecordCount();
          const loaded = table.getLoadedRecordCount();
          if (!Number.isSafeInteger(total) || total < 0 || total > 20000 || loaded !== total ||
              !Array.isArray(ids) || ids.length !== total || new Set(ids).size !== ids.length ||
              ids.some((id) => typeof id !== 'string' || !id)) return null;
          const fields = input.field_ids.map((id) => {
            const value = fieldsById[id];
            if (!value || value.id !== id) throw new Error('missing field');
            const property = value.property || {};
            if (value.type === 24) {
              // Stage is not an ordinary select: retain its terminal options,
              // transition rules and required fields for binding verification.
              return { id: value.id, name: value.name, type: value.type,
                fieldUIType: value.fieldUIType || value.exInfo?.fieldUIType,
                property,
                ...(Object.hasOwn(value, 'defaultValueRule') ? { defaultValueRule: value.defaultValueRule } : {}) };
            }
            return { id: value.id, name: value.name, type: value.type,
              property: { ...(Object.hasOwn(property, 'tableId') ? { tableId: property.tableId } : {}),
                ...(Object.hasOwn(property, 'options') ? { options: property.options.map((x) => ({ id: x.id, name: x.name })) } : {}) } };
          });
          const rows = ids.map((record_id) => {
            const data = table.getRecordDataById(record_id);
            if (!data || typeof data !== 'object') throw new Error('missing record');
            const cells = {};
            for (const id of input.field_ids) {
              const cell = data[id];
              // Record counts alone do not prove each field is hydrated.
              // A missing key cannot be used to erase a project association.
              if (!Object.hasOwn(data, id) || cell === undefined) throw new Error('missing cell');
              if (cell === null) cells[id] = null;
              else if (typeof cell === 'object' && Object.hasOwn(cell, 'value')) cells[id] = { value: cell.value };
              else throw new Error('unsupported cell');
            }
            return { record_id, cells };
          });
          rows.sort((a, b) => a.record_id.localeCompare(b.record_id));
          return JSON.parse(encode({ fields, rows, total, loaded }));
        };
        try {
          const first = capture();
          if (first) {
            // Read the live page model again after yielding to pending updates.
            await pause();
            if (window.bitableStore !== store || window.commentSDK !== sdk ||
                store.modelOperator?.getTableById(input.table_id) !== table) {
              return { status: 'failed', code: 'PAGE_CONTEXT_CHANGED' };
            }
            const second = capture();
            if (String(store.getCurrentUserId()) !== input.user_id) return { status: 'failed', code: 'ACCOUNT_MISMATCH' };
            if (sdk.serviceToken !== input.base_token) return { status: 'failed', code: 'RESOURCE_MISMATCH' };
            if (!second || encode(first) !== encode(second)) return { status: 'failed', code: 'RECORDS_CHANGED' };
            return { status: 'complete', base_token: input.base_token, user_id: input.user_id,
              table_id: input.table_id, data: { ...second, stable: true } };
          }
        } catch {
          return { status: 'failed', code: 'PAGE_INTERFACE_CHANGED' };
        }
      }
    }
    // Do not treat an absent model, permission page, or login screen as an empty Base.
    if (Date.now() >= end) return { status: 'failed', code: 'RECORDS_INCOMPLETE' };
    await pause();
  }
}

export async function readComments(input) {
  const encode = (value) => JSON.stringify(value, (_key, item) =>
    item && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
  const failed = (code = 'COMMENTS_INCOMPLETE', reason) => ({ status: 'failed', code,
    ...(reason ? { diagnostic: { comment_check: reason } } : {}) });
  const store = window.bitableStore;
  const sdk = window.commentSDK;
  const context = () => window.bitableStore === store && window.commentSDK === sdk &&
    String(store?.getCurrentUserId?.()) === input.user_id && sdk?.serviceToken === input.base_token;
  if (!context()) return failed('ACCOUNT_MISMATCH');
  const schedule = sdk.syncV2?.dataSchedule;
  const core = schedule?.core;
  const methods = ['getAllIdsRequestParams', 'getBatchRequestParams', 'isAllDataLoaded',
    'hasDataToConsume', 'hasAllIdsDataToConsume'];
  if (!core || methods.some((name) => typeof core[name] !== 'function') ||
      typeof sdk.batchAllComments !== 'function' || typeof sdk.getData !== 'function' ||
      typeof sdk.data?.getAllComments !== 'function' ||
      ['getAllIds', 'getScheduleErrorIds', 'getScheduleState'].some((name) => typeof schedule[name] !== 'function')) {
    return failed('PAGE_INTERFACE_CHANGED');
  }
  // The inspected native protocol indexes both unresolved and resolved IDs,
  // then requests every indexed comment with replies. Do not substitute the
  // incremental initialization response for this read contract.
  const params = core.getAllIdsRequestParams();
  const batch = core.getBatchRequestParams({ ids: [], noNeedRelations: false });
  if (!params || Object.keys(params).sort().join(',') !== 'comment_type_list,need_comment_relations' ||
      JSON.stringify(params.comment_type_list) !== '[0,1]' || batch?.need_reply !== true ||
      batch?.need_msg_box_version !== true || core.allIdsUrl !== '/api/comment/commentid/all' ||
      core.batchUrl !== '/api/comment/batch') return failed('PAGE_INTERFACE_CHANGED');
  let timer;
  let loaded;
  try {
    loaded = await Promise.race([sdk.batchAllComments(), new Promise((resolve) => {
      timer = setTimeout(() => resolve(false), 25000);
    })]);
  } catch { return failed(undefined, 'batch_failed'); }
  finally { clearTimeout(timer); }
  if (!context()) return failed('PAGE_CONTEXT_CHANGED');
  if (loaded !== true) return failed(undefined, 'batch_not_complete');
  const stableId = (id) => typeof id === 'string' && id.length > 0;
  const zeroSet = (value) => value instanceof Set && value.size === 0;
  const timestamp = (value) => Number.isSafeInteger(value) && value >= 0;
  const capture = () => {
    const indexState = core.allIdsRequestControllInfo;
    const loadState = core.scheduleControllInfo;
    const ids = schedule.getAllIds();
    const errors = schedule.getScheduleErrorIds();
    if (!context() || sdk.hasInit !== true || sdk.syncV2?.dataSchedule !== schedule || schedule.core !== core ||
        schedule.batchAllCommentsForMsgBoxResolve ||
        indexState?.isLoaded !== true || indexState.isError !== false || indexState.requestId ||
        loadState?.isError !== false || loadState.loadingIdsCount !== 0 ||
        core.isAllDataLoaded() !== true || core.hasDataToConsume() !== false ||
        core.hasAllIdsDataToConsume() !== false || !Array.isArray(errors) || errors.length ||
        !zeroSet(core.noPermCommentIdsCache) || !zeroSet(core.noReturnDataCommentIdsCache) ||
        !Array.isArray(ids) || ids.length > 10000 || ids.some((id) => !stableId(id)) ||
        new Set(ids).size !== ids.length || ids.some((id) => schedule.getScheduleState(id) !== 'LOADED')) {
      throw new Error('index_or_load_incomplete');
    }
    if (!(typeof core.miniVersion === 'string' && core.miniVersion) &&
        !Number.isSafeInteger(core.miniVersion)) throw new Error('missing_version');
    const normalized = sdk.getData()?.comments;
    const models = sdk.data.getAllComments();
    if (!Array.isArray(normalized) || !Array.isArray(models) || normalized.length !== ids.length ||
        models.length !== ids.length) throw new Error('index_model_mismatch');
    const index = new Set(ids), modelMap = new Map();
    for (const model of models) {
      if (!stableId(model.commentId) || !index.has(model.commentId) || modelMap.has(model.commentId) ||
          model.unSubmitted || !Array.isArray(model.replyList)) throw new Error('uncommitted_or_unknown_model');
      modelMap.set(model.commentId, model);
    }
    const seen = new Set();
    const threads = normalized.map((thread) => {
      const cid = thread.commentId;
      if (!stableId(cid) || !index.has(cid) || seen.has(cid) || thread.unSubmitted ||
          typeof thread.finished !== 'boolean' || !Array.isArray(thread.replyList) ||
          thread.replyList.length === 0 || thread.replyList.length > 20000) throw new Error('thread_incomplete');
      seen.add(cid);
      const expected = modelMap.get(cid).replyList.map((reply) => reply.replyId);
      const actual = thread.replyList.map((reply) => reply.replyId);
      if (JSON.stringify(expected) !== JSON.stringify(actual) || actual.some((id) => !stableId(id)) ||
          new Set(actual).size !== actual.length) throw new Error('reply_export_mismatch');
      const replies = thread.replyList.map((reply) => {
        if (!stableId(reply.userId) || typeof reply.content !== 'string' ||
            !timestamp(reply.createTime) || !timestamp(reply.updateTime) ||
            reply.deleted || reply.submitting || reply.error) throw new Error('reply_incomplete');
        const location = reply.extra?.notify_extra;
        const extra = location === undefined ? {} : { notify_extra: JSON.parse(JSON.stringify(location)) };
        return { reply_id: reply.replyId, user_id: reply.userId,
          create_time: reply.createTime, update_time: reply.updateTime,
          content: { elements: [{ type: 'text_run', text_run: { text: reply.content } }] }, extra };
      });
      return { comment_id: cid, is_solved: thread.finished,
        solver_user_id: thread.finishUserId || '',
        create_time: thread.createTime, update_time: thread.updateTime, replies };
    });
    threads.sort((a, b) => a.comment_id.localeCompare(b.comment_id));
    return { index: [...ids].sort(), version: core.miniVersion, threads };
  };
  try {
    const before = capture();
    await new Promise((resolve) => setTimeout(resolve, 200));
    if (!context()) return failed('PAGE_CONTEXT_CHANGED');
    const after = capture();
    if (encode(before) !== encode(after)) return failed(undefined, 'comments_changed');
    return { status: 'complete', data: { complete: true, includes_resolved: true, model_replies_complete: true,
      method: 'native_sdk_visible_model',
      scope: 'current_accessible_sdk_index', threads: after.threads } };
  } catch { return failed(undefined, 'index_model_or_reply_check_failed'); }
}
