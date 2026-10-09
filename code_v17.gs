// AN PHÚC EDUCATION • V18 • bổ sung cấu trúc, giữ dữ liệu cũ
const SPREADSHEET_ID = "1k5__Km-LjS7pV3laItIe34IgAEFozP8A3u07zhb4RW4";

const ADMIN_EMAIL = "duclehoang01@gmail.com";

const SCHEMA_VERSION = "2026-10-08-v18-safe";

const QUEUE_STALE_MINUTES = 10;

function jsonResponse(success, dataOrMessage) {
  const res = { success: success };
  if (success) { res.data = dataOrMessage; } else { res.message = dataOrMessage; }
  return ContentService.createTextOutput(JSON.stringify(res)).setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) { return routeRequest(e.parameter, 'GET'); }

function doPost(e) {
  try { return routeRequest(JSON.parse(e.postData.contents), JSON.parse(e.postData.contents)._method==='GET'?'GET':'POST'); } 
  catch (err) { return jsonResponse(false, "Lỗi phân tích JSON"); }
}

function withScriptLock(fn) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) throw new Error('Hệ thống đang có người khác cập nhật dữ liệu. Vui lòng thử lại sau vài giây.');
  try { return fn(); } finally { lock.releaseLock(); }
}

function normalizeNumberString(v) { const n = Number(v); return Number.isFinite(n) ? String(n) : String(v).trim(); }

function normalizeClassName(v) { return String(v || '').trim().toUpperCase(); }

function classCode(grade, className) { return `${normalizeNumberString(grade)}${normalizeClassName(className)}`; }

function formatMoneyVN(amount) {
  const n = Number(amount || 0);
  return Number.isFinite(n) ? n.toLocaleString('vi-VN') : '0';
}

function formatDateTimeSafe(val) { try { return Utilities.formatDate(new Date(val), "GMT+7", "dd/MM/yyyy HH:mm"); } catch(e) { return String(val); } }

function normalizeVietnamPhone(phone) {
  if (!phone) return ""; let s = String(phone).trim(); if (s.endsWith('.0')) s = s.slice(0, -2); s = s.replace(/[^\d]/g, '');
  if (s.startsWith('84') && (s.length === 11 || s.length === 10)) s = '0' + s.slice(2);
  if (s.length > 0 && s[0] !== '0') s = '0' + s; return /^0[3|5|7|8|9][0-9]{8}$/.test(s) ? s : ""; 
}

function generateId(prefix) { return prefix + Date.now() + Math.floor(Math.random() * 1000); }

function processZaloQueue() {
  ensureSchema();
  if(PropertiesService.getScriptProperties().getProperty('ZALO_SENDING_ENABLED')!=='true')return {paused:true,processed:0,message:'Chưa bật gửi Zalo thật.'};
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName('MessageLogs');
  if (!sheet) return { success: false, message: 'Sheet MessageLogs không tồn tại' };

  const token = Utilities.getUuid();
  const claimedRows = [];
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(12000)) return { success: true, processed: 0, busy: true };
  try {
    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) return { success: true, processed: 0 };
    const h = data[0];
    const idx = name => h.indexOf(name);
    const statusIdx = idx('status'), tokenIdx = idx('processing_token'), processingAtIdx = idx('processing_at'), attemptIdx = idx('attempt_count');
    const now = new Date();

    // Thu hồi các job PROCESSING bị treo quá lâu.
    for (let i = 1; i < data.length; i++) {
      if (String(data[i][statusIdx] || '').toUpperCase() !== 'PROCESSING') continue;
      const started = processingAtIdx >= 0 ? new Date(data[i][processingAtIdx]) : null;
      if (started && !isNaN(started.getTime()) && (now.getTime() - started.getTime()) > QUEUE_STALE_MINUTES * 60000) {
        sheet.getRange(i + 1, statusIdx + 1).setValue('PENDING');
        if (tokenIdx >= 0) sheet.getRange(i + 1, tokenIdx + 1).clearContent();
        if (processingAtIdx >= 0) sheet.getRange(i + 1, processingAtIdx + 1).clearContent();
        data[i][statusIdx] = 'PENDING';
      }
    }

    for (let i = 1; i < data.length && claimedRows.length < 6; i++) {
      if (String(data[i][statusIdx] || '').toUpperCase() !== 'PENDING') continue;
      sheet.getRange(i + 1, statusIdx + 1).setValue('PROCESSING');
      if (tokenIdx >= 0) sheet.getRange(i + 1, tokenIdx + 1).setValue(token);
      if (processingAtIdx >= 0) sheet.getRange(i + 1, processingAtIdx + 1).setValue(now);
      if (attemptIdx >= 0) sheet.getRange(i + 1, attemptIdx + 1).setValue(Number(data[i][attemptIdx] || 0) + 1);
      claimedRows.push(i + 1);
    }
    SpreadsheetApp.flush();
  } finally { lock.releaseLock(); }

  if (!claimedRows.length) return { success: true, processed: 0 };

  let processedCount = 0, successCount = 0, failedCount = 0;
  const students = getSheetDataAsObjects('Students');

  claimedRows.forEach(rowNumber => {
    const current = sheet.getRange(rowNumber, 1, 1, sheet.getLastColumn()).getValues()[0];
    const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    const idx = name => headers.indexOf(name);
    if (String(current[idx('processing_token')] || '') !== token) return;

    const logId = current[idx('log_id')];
    const stableRequestId = idx('request_id') >= 0 ? String(current[idx('request_id')] || logId) : String(logId);
    const phone = current[idx('phone')];
    const msg = current[idx('message')];
    let zaloName = String(current[idx('zalo_name')] || '').trim();
    const studentId = current[idx('student_id')];
    const displayName = current[idx('name')];
    const messageType = String(current[idx('message_type')] || '');
    let className = idx('class_name') >= 0 ? String(current[idx('class_name')] || '').trim() : '';

    if (studentId && !String(studentId).startsWith('CLASS_')) {
      const student = students.find(s => String(s.student_id) === String(studentId));
      if (student) {
        if (!zaloName) zaloName = String(student.zalo_name || '').trim();
        if (!className) className = `K${student.grade}-${student.class}`;
      }
    }

    const context = {
      student_id: studentId,
      student_name: displayName,
      class_name: className,
      recipient_type: messageType === 'CLASS_SESSION_REPORT' ? 'group' : 'parent'
    };
    const res = sendZaloViaGateway(phone, msg, zaloName, context, stableRequestId);

    // Chỉ worker đang sở hữu token mới được ghi kết quả.
    const finishLock = LockService.getScriptLock();
    if (finishLock.tryLock(10000)) {
      try {
        const tokenCell = idx('processing_token') >= 0 ? sheet.getRange(rowNumber, idx('processing_token') + 1).getValue() : token;
        if (String(tokenCell || '') === token) {
          sheet.getRange(rowNumber, idx('status') + 1).setValue(res.gateway_response && ['SENT_UNCONFIRMED','RESULT_UNKNOWN'].includes(res.gateway_response.status) ? 'NEEDS_REVIEW' : (res.success ? 'SENT' : 'FAILED'));
          if (idx('sent_at') >= 0 && res.success) sheet.getRange(rowNumber, idx('sent_at') + 1).setValue(new Date());
          if (idx('error') >= 0) sheet.getRange(rowNumber, idx('error') + 1).setValue(res.gateway_response && ['SENT_UNCONFIRMED','RESULT_UNKNOWN'].includes(res.gateway_response.status) ? 'Chưa xác nhận được kết quả; cần đối chiếu trên Zalo.' : (res.success ? '' : (res.message || 'SEND_FAILED')));
          if (idx('http_status') >= 0) sheet.getRange(rowNumber, idx('http_status') + 1).setValue(res.http_status || '');
          if (idx('gateway_response') >= 0) sheet.getRange(rowNumber, idx('gateway_response') + 1).setValue(JSON.stringify(res.gateway_response || res.message));
          if (idx('processing_token') >= 0) sheet.getRange(rowNumber, idx('processing_token') + 1).clearContent();
          if (idx('processing_at') >= 0) sheet.getRange(rowNumber, idx('processing_at') + 1).clearContent();
        }
      } finally { finishLock.releaseLock(); }
    }
    processedCount++;
    if (res.success) successCount++; else failedCount++;
  });

  return { success: true, processed: processedCount, sent: successCount, failed: failedCount };
}

function getZaloSendUrl() {
  const configured = PropertiesService.getScriptProperties().getProperty('ZALO_GATEWAY_URL');
  if (!configured) throw new Error('Chưa cấu hình ZALO_GATEWAY_URL');
  const url = configured.trim().replace(/\/+$/, '');
  return url.endsWith('/gui-tin-zalo') ? url : url + '/gui-tin-zalo';
}

function sendZaloViaGateway(phone, message, zaloName, context, requestId) {
  const url = getZaloSendUrl();
  const ctx = context || {};
  const payload = {
    so_dien_thoai: normalizeVietnamPhone(phone) || '',
    noi_dung: message,
    ten_zalo: String(zaloName || '').trim(),
    student_id: ctx.student_id || '',
    ten_hoc_sinh: ctx.student_name || '',
    ten_lop: ctx.class_name || '',
    recipient_type: ctx.recipient_type || 'parent',
    request_id: requestId || ''
  };
  const options = { method: 'post', contentType: 'application/json', payload: JSON.stringify(payload), headers: { "ngrok-skip-browser-warning": "true", "X-Gateway-Token":PropertiesService.getScriptProperties().getProperty("ZALO_GATEWAY_TOKEN")||"" }, muteHttpExceptions: true };
  try {
    const response = UrlFetchApp.fetch(url, options); const statusCode = response.getResponseCode(); const resText = response.getContentText();
    let resJson; try { resJson = JSON.parse(resText); } catch(e) { return { success: false, message: `Lỗi Zalo: ${resText.slice(0, 100)}`, http_status: statusCode }; }
    if (statusCode >= 200 && statusCode < 300 && resJson.success === true) { return { success: true, gateway_response: resJson, http_status: statusCode }; }
    return { success: false, message: resJson.message || `HTTP ${statusCode}`, http_status: statusCode, gateway_response: resJson };
  } catch (error) { return { success: false, message: "Lỗi gọi Gateway: " + error.message }; }
}

const V17_CACHE_TTL = 600;

const V17_CACHE_KEYS = { SETTINGS:'V17_SETTINGS', CLASSES:'V17_CLASSES', TEMPLATES:'V17_TEMPLATES' };

let V17_REQ_CACHE = {};

function v17Bool(v) { return v === true || String(v || '').trim().toUpperCase() === 'TRUE' || String(v || '').trim() === '1'; }

function v17NowIso() { return Utilities.formatDate(new Date(), 'GMT+7', "yyyy-MM-dd'T'HH:mm:ssXXX"); }

function v17JsonClone(v) { return JSON.parse(JSON.stringify(v)); }

function v17Invalidate(name) {
  const c = CacheService.getScriptCache();
  if (!name || name === 'settings') c.remove(V17_CACHE_KEYS.SETTINGS);
  if (!name || name === 'classes') c.remove(V17_CACHE_KEYS.CLASSES);
  if (!name || name === 'templates') c.remove(V17_CACHE_KEYS.TEMPLATES);
  V17_REQ_CACHE = {};
}

function v17ReadSheet(sheetName) {
  if (Object.prototype.hasOwnProperty.call(V17_REQ_CACHE, sheetName)) return V17_REQ_CACHE[sheetName];
  const sh = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(sheetName);
  if (!sh || sh.getLastRow() < 1) return (V17_REQ_CACHE[sheetName] = {sheet:sh, headers:[], rows:[], objects:[]});
  const values = sh.getDataRange().getValues();
  const headers = (values[0] || []).map(x => String(x || '').trim());
  const rows = values.slice(1);
  const objects = rows.map(row => { const o={}; headers.forEach((h,i)=>o[h]=row[i]); return o; });
  return (V17_REQ_CACHE[sheetName] = {sheet:sh, headers, rows, objects});
}

function getSheetData(sheetName) { return v17ReadSheet(sheetName).objects.map(x => Object.assign({}, x)); }

function getSheetDataAsObjects(sheetName) { return getSheetData(sheetName); }

function v17HeaderMap(sheetName) { const h=v17ReadSheet(sheetName).headers; const out={}; h.forEach((x,i)=>out[x]=i); return out; }

function v17WriteObjectRow(sheetName, obj) {
  const info=v17ReadSheet(sheetName), sh=info.sheet, headers=info.headers;
  if (!sh) throw new Error('Sheet '+sheetName+' không tồn tại');
  const row=headers.map(h => Object.prototype.hasOwnProperty.call(obj,h) ? obj[h] : '');
  sh.appendRow(row); V17_REQ_CACHE = {}; return row;
}

function v17EnsureSheet(ss, name, headers) {
  let sh=ss.getSheetByName(name);
  if(!sh){ sh=ss.insertSheet(name); sh.getRange(1,1,1,headers.length).setValues([headers]).setFontWeight('bold').setBackground('#e3f2fd'); return sh; }
  const last=Math.max(sh.getLastColumn(),1);
  const existing=sh.getRange(1,1,1,last).getValues()[0].map(v=>String(v||'').trim());
  const missing=headers.filter(h=>existing.indexOf(h)<0);
  if(missing.length){ sh.getRange(1, sh.getLastColumn()+1, 1, missing.length).setValues([missing]).setFontWeight('bold').setBackground('#e3f2fd'); }
  return sh;
}

function v17InferCurrentClasses(ss) {
  const seen={};
  ['Students','Attendance','ClassSessions'].forEach(name=>{
    const sh=ss.getSheetByName(name); if(!sh || sh.getLastRow()<2) return;
    const vals=sh.getDataRange().getValues(), h=vals[0].map(x=>String(x||'').trim());
    const gi=h.indexOf('grade'), ci=h.indexOf('class'); if(gi<0||ci<0) return;
    vals.slice(1).forEach(r=>{ const g=normalizeNumberString(r[gi]), c=normalizeClassName(r[ci]); if(g&&c) seen[`${g}|${c}`]={grade:g,class_code:c}; });
  });
  let arr=Object.keys(seen).map(k=>seen[k]);
  if(!arr.length){ [6,7,8,9].forEach(g=>['A','B'].forEach(c=>arr.push({grade:String(g),class_code:c}))); }
  arr.sort((a,b)=>(Number(a.grade)-Number(b.grade))||a.class_code.localeCompare(b.class_code));
  return arr;
}

function v17ClassId(grade, classCode) { return `K${normalizeNumberString(grade)}_${normalizeClassName(classCode).replace(/[^A-Z0-9]+/g,'_')}`; }

function initSheets() {
  const ss=SpreadsheetApp.openById(SPREADSHEET_ID);
  const specs=[
    ['Students',['student_id','name','grade','class','parent_phone','email','note','created_at','custom_fee','zalo_name','enrollment_date','class_id','active','row_version']],
    ['Attendance',['attendance_id','student_id','date','status','grade','class','class_id','row_version']],
    ['Tuition',['tuition_id','student_id','month','year','amount','method','paid_at','grade','class','class_id','client_request_id']],
    ['Materials',['student_id','batch1','batch2','batch3','batch4','batch5','batch6']],
    ['Settings',['key','value']],
    ['MessageLogs',['log_id','request_id','student_id','class_id','name','class_name','phone','zalo_name','zalo_group_name','message_type','month','year','message','status','error','http_status','gateway_response','created_at','sent_at','processing_token','processing_at','attempt_count']],
    ['ClassSessions',['session_id','grade','class','date','created_at','class_id']],
    ['ClassConfig',['class_id','grade','class_code','class_name','zalo_group_name','teacher_name','tuition_fee','schedule','start_time','school_year','active','display_order','note','updated_at','row_version']],
    ['MessageTemplates',['template_key','template_text','active','updated_at']]
  ];
  specs.forEach(x=>v17EnsureSheet(ss,x[0],x[1]));

  const classSh=ss.getSheetByName('ClassConfig');
  if(classSh.getLastRow()===1){
    const current=v17InferCurrentClasses(ss);
    const year=new Date().getFullYear();
    const rows=current.map((x,i)=>[v17ClassId(x.grade,x.class_code),x.grade,x.class_code,`Toán ${x.grade}${x.class_code}`,'','','','','',`${year}-${year+1}`,true,(i+1)*10,'',new Date()]);
    if(rows.length){ const keys=['class_id','grade','class_code','class_name','zalo_group_name','teacher_name','tuition_fee','schedule','start_time','school_year','active','display_order','note','updated_at']; const h=classSh.getRange(1,1,1,classSh.getLastColumn()).getValues()[0]; rows.forEach(r=>classSh.appendRow(h.map(k=>keys.includes(k)?r[keys.indexOf(k)]:''))); }
  }
  const tplSh=ss.getSheetByName('MessageTemplates');
  if(tplSh.getLastRow()===1){
    const defaults=[
      ['ATTENDANCE_PRESENT','Trung tâm An Phúc xin thông báo: Học sinh {student_name} CÓ MẶT tại lớp {class_name} vào ngày {date}.',true,new Date()],
      ['ATTENDANCE_ABSENT','Trung tâm An Phúc xin thông báo: Học sinh {student_name} {attendance_status} tại lớp {class_name} vào ngày {date}.',true,new Date()],
      ['PAYMENT_REMINDER','Trung tâm An Phúc xin thông báo: Học phí tháng {month}/{year} của học sinh {student_name} là {amount} đồng. Hiện trung tâm chưa ghi nhận khoản học phí này. Kính mong phụ huynh kiểm tra và hoàn thành giúp trung tâm. Xin cảm ơn!',true,new Date()],
      ['PAYMENT_RECEIVED','Trung tâm An Phúc xin thông báo: Ngày {date}, Trung tâm đã ghi nhận học phí tháng {month}/{year} của học sinh {student_name}, số tiền {amount} đồng, hình thức {method}. Cảm ơn Quý phụ huynh!',true,new Date()],
      ['MONTHLY_LESSON_REPORT','Trung tâm An Phúc Education xin thông báo: Trong tháng {month}/{year}, lớp {class_name} đã học {sessions} buổi. Các ngày học: {dates}. Trân trọng!',true,new Date()],
      ['CLASS_SCHEDULE_CHANGE','Trung tâm An Phúc xin thông báo lịch học lớp {class_name}: {schedule} {start_time}.',true,new Date()]
    ];
    const h=tplSh.getRange(1,1,1,tplSh.getLastColumn()).getValues()[0]; const keys=['template_key','template_text','active','updated_at']; defaults.forEach(r=>tplSh.appendRow(h.map(k=>keys.includes(k)?r[keys.indexOf(k)]:'')));
  }

  v17Invalidate();
}

function getSettings() {
  const rows=v17CachedObjects('Settings',V17_CACHE_KEYS.SETTINGS), out={}; rows.forEach(r=>out[r.key]=r.value);
  if(!out.config_version) out.config_version='1'; return out;
}

function v17BumpConfigVersion() {
  const sh=SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName('Settings'), data=sh.getDataRange().getValues(); let row=-1;
  for(let i=1;i<data.length;i++) if(String(data[i][0])==='config_version'){row=i+1;break;}
  const next=String(Number(row>0?data[row-1][1]:0)+1); if(row>0)sh.getRange(row,2).setValue(next); else sh.appendRow(['config_version',next]); v17Invalidate('settings'); return next;
}

function getClassById(id) { return getClassConfig({}).find(x=>String(x.class_id)===String(id)) || null; }

function getClassByLegacy(grade,className) { return getClassConfig({}).find(x=>normalizeNumberString(x.grade)===normalizeNumberString(grade)&&normalizeClassName(x.class_code)===normalizeClassName(className)) || null; }

function getClassZaloName(grade,className){ const c=getClassByLegacy(grade,className); return c ? String(c.zalo_group_name||'').trim() : ''; }

function v17ResolveClass(params) {
  const id=String(params.class_id||'').trim(); if(id){ const c=getClassById(id); if(c)return c; }
  return getClassByLegacy(params.grade, params.className || params.class || '');
}

function setClassActive(params) { const c=getClassById(params.class_id); if(!c)throw new Error('Không tìm thấy lớp'); return saveClassConfig(Object.assign({},c,{active:v17Bool(params.active)})); }

function getMessageTemplates(){ return v17CachedObjects('MessageTemplates',V17_CACHE_KEYS.TEMPLATES); }

function saveMessageTemplate(params){ const key=String(params.template_key||'').trim(); const text=String(params.template_text||'').trim(); if(!key||!text)throw new Error('Thiếu template_key/template_text'); const sh=SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName('MessageTemplates'), data=sh.getDataRange().getValues(), h=data[0].map(x=>String(x||'').trim()); let row=-1; for(let i=1;i<data.length;i++)if(String(data[i][h.indexOf('template_key')])===key){row=i+1;break;} const obj={template_key:key,template_text:text,active:params.active===false?false:true,updated_at:new Date()}; if(row>0)v17UpdateRow('MessageTemplates',row,obj); else v17WriteObjectRow('MessageTemplates',obj); v17Invalidate('templates'); v17BumpConfigVersion(); return {saved:true}; }

function renderMessageTemplate(key, vars, fallback) { const t=getMessageTemplates().find(x=>String(x.template_key)===String(key)&&v17Bool(x.active)); let text=String(t&&t.template_text?t.template_text:fallback||''); Object.keys(vars||{}).forEach(k=>{ text=text.split('{'+k+'}').join(String(vars[k]??'')); }); return text; }

function getBootstrapData(params) {
  const settings=getSettings(), classConfig=getClassConfig({active_only:'true'}), now=new Date(), month=now.getMonth()+1, year=now.getFullYear();
  const students=getSheetData('Students'); const payments=getSheetData('Tuition').filter(t=>normalizeNumberString(t.month)===String(month)&&normalizeNumberString(t.year)===String(year));
  return {settings,classConfig,dashboardSummary:{total_students:students.length,paid_count:new Set(payments.map(x=>String(x.student_id))).size,unpaid_count:Math.max(0,students.length-new Set(payments.map(x=>String(x.student_id))).size)},config_version:settings.config_version||'1',server_time:v17NowIso()};
}

function buildTuitionReminderMessage(student,month,year,amount){ return renderMessageTemplate('PAYMENT_REMINDER',{student_name:student.name,class_name:student.class_name||`K${student.grade}-${student.class}`,month,year,amount:formatMoneyVN(amount)},`Trung tâm An Phúc xin thông báo: Học phí tháng ${month}/${year} của học sinh ${student.name} là ${formatMoneyVN(amount)} đồng. Hiện trung tâm chưa ghi nhận khoản học phí này. Kính mong phụ huynh kiểm tra và hoàn thành giúp trung tâm. Xin cảm ơn!`); }

function queueZaloMessage(student_id,name,phone,type,month,year,msg,zaloName,className,classId) {
  const sheet=SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName('MessageLogs'), info=v17ReadSheet('MessageLogs'); if(!sheet)throw new Error('Sheet MessageLogs không tồn tại');
  const stableRequestId = type==='PAYMENT_REMINDER' ? `TUITION_${student_id}_${year}_${month}` : type==='CLASS_SESSION_REPORT' ? `LESSON_${classId||student_id}_${year}_${month}` : `${type}_${student_id}_${Utilities.base64EncodeWebSafe(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,String(msg))).slice(0,18)}`;
  const statuses=new Set(['PENDING','PROCESSING','SENT','NEEDS_REVIEW']); const existing=info.objects.find(r=>String(r.request_id||'')===stableRequestId&&statuses.has(String(r.status||'').toUpperCase())); if(existing)return {log_id:existing.log_id,request_id:stableRequestId,duplicate:true};
  const logId=generateId('LOG'); v17WriteObjectRow('MessageLogs',{log_id:logId,request_id:stableRequestId,student_id:student_id||'',class_id:classId||'',name:name||'',class_name:className||'',phone:phone||'',zalo_name:zaloName||'',zalo_group_name:type==='CLASS_SESSION_REPORT'?(zaloName||''):'',message_type:type,month:month||'',year:year||'',message:msg,status:'PENDING',created_at:new Date(),attempt_count:0}); return {log_id:logId,request_id:stableRequestId,duplicate:false};
}

function getLessonReport(params){ const targetMonth=Number(params.month),targetYear=Number(params.year),classes=getClassConfig({active_only:'true'}),att=getSheetData('Attendance'),sessions=getSheetData('ClassSessions'),out=[]; classes.forEach(c=>{if(params.grade&&normalizeNumberString(c.grade)!==normalizeNumberString(params.grade))return;if(params.class_id&&String(c.class_id)!==String(params.class_id))return;if(!params.class_id&&params.className&&normalizeClassName(c.class_code)!==normalizeClassName(params.className))return;const dates=new Set(); const add=x=>{const p=parseDateHelper(x.date);if(p&&p.year===targetYear&&p.month===targetMonth)dates.add(p.dateStr);}; sessions.filter(x=>String(x.class_id||'')===String(c.class_id)||(normalizeNumberString(x.grade)===normalizeNumberString(c.grade)&&normalizeClassName(x.class)===normalizeClassName(c.class_code))).forEach(add); att.filter(x=>String(x.class_id||'')===String(c.class_id)||(normalizeNumberString(x.grade)===normalizeNumberString(c.grade)&&normalizeClassName(x.class)===normalizeClassName(c.class_code))).forEach(add); const arr=Array.from(dates).sort(); if(arr.length||!params.only_with_sessions)out.push({class_id:c.class_id,grade:String(c.grade),class_code:String(c.class_code),class_name:c.class_name||`K${c.grade}-${c.class_code}`,total_sessions:arr.length,dates_str:arr.map(d=>{const a=d.split('-');return `${a[2]}/${a[1]}`;}).join(', '),zalo_name:String(c.zalo_group_name||'')}); }); return out; }

function buildClassLessonMessage(item,month,year){return renderMessageTemplate('MONTHLY_LESSON_REPORT',{class_name:item.class_name,month,year,sessions:item.total_sessions,dates:item.dates_str||''},`Trung tâm An Phúc Education xin thông báo: Trong tháng ${month}/${year}, lớp ${item.class_name} đã học ${item.total_sessions} buổi. Các ngày học: ${item.dates_str||''}. Trân trọng!`);}

function hasPendingClassLessonMessage(grade,className,month,year,classId){const rid=`LESSON_${classId||(getClassByLegacy(grade,className)||{}).class_id||v17ClassId(grade,className)}_${year}_${month}`;return getSheetData('MessageLogs').some(r=>String(r.request_id||'')===rid&&['PENDING','PROCESSING','SENT'].includes(String(r.status||'').toUpperCase()));}

function sendClassLessonReport(params){const c=v17ResolveClass(params);if(!c)throw new Error('Không tìm thấy lớp');const month=normalizeNumberString(params.month),year=normalizeNumberString(params.year),z=String(c.zalo_group_name||'').trim();if(!z){const e=new Error('Lớp này chưa cấu hình nhóm Zalo.');e.code='ZALO_GROUP_NOT_CONFIGURED';throw e;}const report=getLessonReport({class_id:c.class_id,month,year,only_with_sessions:true});if(!report.length)throw new Error(`Tháng ${month}/${year} chưa có dữ liệu buổi học của lớp ${c.class_name}.`);const item=report[0];const msg=buildClassLessonMessage(item,month,year);const q=queueZaloMessage(`CLASS_${c.class_id}`,c.class_name,'','CLASS_SESSION_REPORT',month,year,msg,z,c.class_name,c.class_id);return {queued:q.duplicate?0:1,duplicate:q.duplicate,class_id:c.class_id,class_name:c.class_name,total_sessions:item.total_sessions,zalo_name:z,message_text:msg};}

function sendBulkClassLessonReports(params){const month=normalizeNumberString(params.month),year=normalizeNumberString(params.year),report=getLessonReport({month,year,grade:params.grade||'',className:params.className||'',class_id:params.class_id||'',only_with_sessions:true});let queued=0,skipped_no_zalo=0,duplicate=0;const details=[];report.forEach(item=>{if(!item.zalo_name){skipped_no_zalo++;details.push({class_id:item.class_id,class_name:item.class_name,status:'ZALO_GROUP_NOT_CONFIGURED'});return;}const q=queueZaloMessage(`CLASS_${item.class_id}`,item.class_name,'','CLASS_SESSION_REPORT',month,year,buildClassLessonMessage(item,month,year),item.zalo_name,item.class_name,item.class_id);if(q.duplicate){duplicate++;details.push({class_id:item.class_id,class_name:item.class_name,status:'DUPLICATE'});}else{queued++;details.push({class_id:item.class_id,class_name:item.class_name,status:'QUEUED',zalo_name:item.zalo_name});}});return {queued,skipped_no_zalo,duplicate,total:report.length,details};}

function testZaloGroup(params){const c=getClassById(params.class_id)||v17ResolveClass(params);if(!c)throw new Error('Không tìm thấy lớp');const group=String(c.zalo_group_name||'').trim();if(!group)return {found:false,status:'ZALO_GROUP_NOT_CONFIGURED',message:'Lớp này chưa cấu hình nhóm Zalo.'};const configured=PropertiesService.getScriptProperties().getProperty('ZALO_GATEWAY_URL');if(!configured)throw new Error('Chưa cấu hình ZALO_GATEWAY_URL');const base=String(configured).trim().replace(/\/+$/,'').replace(/\/gui-tin-zalo$/,'');const url=base+'/kiem-tra-zalo';try{const res=UrlFetchApp.fetch(url,{method:'post',contentType:'application/json',payload:JSON.stringify({zalo_name:group,recipient_type:'group'}),headers:{'X-Gateway-Token':PropertiesService.getScriptProperties().getProperty('ZALO_GATEWAY_TOKEN')||''},muteHttpExceptions:true});const body=res.getContentText();let js={};try{js=JSON.parse(body);}catch(e){}return {found:!!js.found,status:js.status||(js.found?'FOUND':'NOT_FOUND'),message:js.message||(js.found?'Đã tìm thấy':'Không tìm thấy nhóm Zalo'),gateway_http:res.getResponseCode()};}catch(e){return {found:false,status:'GATEWAY_ERROR',message:e.message};}}

function setup() {
  return withScriptLock(() => {
    V17_REQ_CACHE = {};
    initSheets();
    PropertiesService.getScriptProperties().setProperty('SCHEMA_VERSION', SCHEMA_VERSION);
    return {success:true, message:'Đã bổ sung cấu trúc còn thiếu. Không ghi lại dữ liệu lịch sử.', sheets:SpreadsheetApp.openById(SPREADSHEET_ID).getSheets().map(s=>s.getName())};
  });
}

function seup() { return setup(); }

function onOpen() {
  SpreadsheetApp.getUi().createMenu('An Phúc • Quản lý học sinh').addItem('Bổ sung sheet/cột an toàn', 'setup').addItem('Điền nhóm Zalo cũ vào ô còn trống', 'importClassConfigSeed').addToUi();
}

function ensureSchema() {
  if(PropertiesService.getScriptProperties().getProperty('SCHEMA_VERSION')!==SCHEMA_VERSION) setup();
}

function v18Error(message, code) { const e=new Error(message); e.code=code||'VALIDATION_ERROR'; return e; }

function v18NowMonth() { const a=Utilities.formatDate(new Date(),'GMT+7','yyyy-MM').split('-'); return {year:Number(a[0]),month:Number(a[1]),key:a.join('-')}; }

function v18Period(month,year) {
  const m=Number(month),y=Number(year);
  if(!Number.isInteger(m)||m<1||m>12||!Number.isInteger(y)||y<2000||y>2100) throw v18Error('Tháng/năm không hợp lệ.');
  return {month:m,year:y,key:`${y}-${String(m).padStart(2,'0')}`};
}

function parseDateHelper(val) {
  if(!val) return null;
  let s=val instanceof Date?Utilities.formatDate(val,'GMT+7','yyyy-MM-dd'):String(val).trim();
  let a=s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})(?:$|[T\s])/);
  if(!a){ const b=s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:$|\s)/); if(b)a=[b[0],b[3],b[2],b[1]]; }
  if(!a){const d=new Date(s);if(isNaN(d.getTime()))return null;return parseDateHelper(d);}
  const y=Number(a[1]),m=Number(a[2]),d=Number(a[3]),check=new Date(Date.UTC(y,m-1,d));
  if(check.getUTCFullYear()!==y||check.getUTCMonth()+1!==m||check.getUTCDate()!==d)return null;
  return {year:y,month:m,dateStr:`${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`};
}

function v17UpdateRow(name,rowNumber,updates) {
  const info=v17ReadSheet(name);
  Object.keys(updates).forEach(k=>{const i=info.headers.indexOf(k);if(i>=0)info.sheet.getRange(rowNumber,i+1).setValue(updates[k]);});
  V17_REQ_CACHE={};
}

function v17CachedObjects(name,key) { return getSheetData(name); }

function getClassConfig(params) {
  let rows=getSheetData('ClassConfig').filter(r=>String(r.class_id||'').trim()).map(r=>Object.assign({},r,{active:r.active===''||r.active==null?true:v17Bool(r.active),display_order:Number(r.display_order||0),tuition_fee:r.tuition_fee===''||r.tuition_fee==null?'':Number(r.tuition_fee)}));
  if(params&&String(params.active_only)==='true')rows=rows.filter(r=>r.active);
  if(params&&params.grade)rows=rows.filter(r=>normalizeNumberString(r.grade)===normalizeNumberString(params.grade));
  return rows.sort((a,b)=>(a.display_order-b.display_order)||(Number(a.grade)-Number(b.grade))||String(a.class_code).localeCompare(String(b.class_code)));
}

function v18StudentClass(s) { return (s.class_id&&getClassById(s.class_id))||getClassByLegacy(s.grade,s.class); }

function v18MatchesClass(row,c) { return row.class_id?String(row.class_id)===String(c.class_id):normalizeNumberString(row.grade)===normalizeNumberString(c.grade)&&normalizeClassName(row.class)===normalizeClassName(c.class_code); }

function getStudents(params) {
  params=params||{};
  const norm=x=>String(x||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/đ/g,'d').replace(/Đ/g,'D').toLowerCase().trim();
  let rows=getSheetData('Students').filter(s=>String(s.student_id||'').trim());
  if(String(params.include_inactive)!=='true')rows=rows.filter(s=>s.active!==false&&String(s.active).toLowerCase()!=='false');
  rows.forEach(s=>{const c=v18StudentClass(s);s.class_id=c?c.class_id:s.class_id||'';s.class_name=c?c.class_name:`K${s.grade}-${s.class}`;s.row_version=Number(s.row_version||0);});
  if(params.grade)rows=rows.filter(s=>normalizeNumberString(s.grade)===normalizeNumberString(params.grade));
  if(params.class_id)rows=rows.filter(s=>String(s.class_id)===String(params.class_id));else if(params.className)rows=rows.filter(s=>normalizeClassName(s.class)===normalizeClassName(params.className));
  if(params.term)rows=rows.filter(s=>[s.name,s.parent_phone,s.zalo_name,s.email].some(v=>norm(v).includes(norm(params.term))));
  return rows.sort((a,b)=>(Number(a.grade)-Number(b.grade))||String(a.class||'').localeCompare(String(b.class||''))||String(a.name||'').localeCompare(String(b.name||''),'vi'));
}

function getExpectedTuitionAmount(s,defaultFee) {
  const fee=v=>v!==''&&v!==undefined&&v!==null&&Number.isFinite(Number(v))&&Number(v)>=0?Number(v):null;
  const custom=fee(s&&s.custom_fee);if(custom!==null)return custom;
  const c=s&&v18StudentClass(s),classFee=fee(c&&c.tuition_fee);if(classFee!==null)return classFee;
  return fee(defaultFee)||0;
}

function v18FeeKnown(s) {const c=v18StudentClass(s),settings=getSettings();return [s.custom_fee,c&&c.tuition_fee,settings.tuition_amount_default].some(x=>x!==''&&x!==null&&x!==undefined&&Number.isFinite(Number(x))&&Number(x)>=0);}

function saveClassConfig(p) {
  const info=v17ReadSheet('ClassConfig'),rows=info.objects;
  const grade=Number(p.grade),code=normalizeClassName(p.class_code||p.className),name=String(p.class_name||'').trim();
  if(!Number.isInteger(grade)||grade<1||grade>12||!code||!name)throw v18Error('Khối phải từ 1 đến 12; mã lớp và tên lớp không được trống.');
  const fee=p.tuition_fee===''||p.tuition_fee==null?'':Number(p.tuition_fee);if(fee!==''&&(!Number.isSafeInteger(fee)||fee<0))throw v18Error('Học phí phải là số nguyên >= 0.');
  const id=String(p.class_id||v17ClassId(grade,code)),i=rows.findIndex(x=>String(x.class_id)===id),old=i>=0?rows[i]:null;
  if(!p.class_id&&old)throw v18Error('Mã lớp đã tồn tại.');
  if(rows.some(x=>String(x.class_id)!==id&&normalizeNumberString(x.grade)===String(grade)&&normalizeClassName(x.class_code)===code))throw v18Error('Khối và mã lớp đã được sử dụng.');
  if(old&&(Number(old.grade)!==grade||normalizeClassName(old.class_code)!==code))throw v18Error('Giữ nguyên khối/mã lớp để bảo toàn liên kết dữ liệu cũ. Tạo lớp mới nếu cần đổi.');
  if(old&&p.expected_version!==undefined&&Number(p.expected_version)!==Number(old.row_version||0))throw v18Error('Cấu hình lớp vừa được người khác sửa. Hãy tải lại.', 'CONFLICT');
  const obj={class_id:id,grade:String(grade),class_code:code,class_name:name,zalo_group_name:String(p.zalo_group_name||'').trim(),teacher_name:String(p.teacher_name||'').trim(),tuition_fee:fee,schedule:String(p.schedule||'').trim(),start_time:String(p.start_time||'').trim(),school_year:String(p.school_year||'').trim(),active:!(p.active===false||String(p.active).toLowerCase()==='false'),display_order:Number(p.display_order||0),note:String(p.note||'').trim(),updated_at:new Date(),row_version:Number(old&&old.row_version||0)+1};
  if(old)v17UpdateRow('ClassConfig',i+2,obj);else v17WriteObjectRow('ClassConfig',obj);v17Invalidate();return {saved:true,class_id:id,row_version:obj.row_version};
}

function importClassConfigSeed() {
  const names={K6_A:'An Phúc_2728_Toán 6A_NC',K6_B:'An Phúc_Toán 6B_2728',K7_A:'An Phúc_Toán 7A_2627_NC',K7_B:'An Phúc_Toán 7B_CB_2627',K8_A:'An Phúc_Toán 8A_NC_2728',K8_B:'An Phúc_Toán 8B_CB_27_28',K9_A:'An Phúc_Toán 9A_2627_NC',K9_B:'An Phúc_Toán 9B_CB_2728'};
  return withScriptLock(()=>{V17_REQ_CACHE={};let added=0;v17ReadSheet('ClassConfig').objects.forEach((r,i)=>{if(!String(r.zalo_group_name||'').trim()&&names[r.class_id]){v17UpdateRow('ClassConfig',i+2,{zalo_group_name:names[r.class_id]});added++;}});v17Invalidate();return {updated:added};});
}

function deleteStudent(p) {
  const info=v17ReadSheet('Students'),i=info.objects.findIndex(s=>String(s.student_id)===String(p.id));if(i<0)throw v18Error('Không tìm thấy học sinh');
  v17UpdateRow('Students',i+2,{active:false,row_version:Number(info.objects[i].row_version||0)+1});return {deleted:false,archived:true};
}

function updateStudent(p) {
  const info=v17ReadSheet('Students'),i=info.objects.findIndex(s=>String(s.student_id)===String(p.id));if(i<0)throw v18Error('Không tìm thấy học sinh');
  const old=info.objects[i],c=v17ResolveClass(p);if(!c)throw v18Error('Lớp không hợp lệ');
  if(p.expected_version!==undefined&&Number(p.expected_version)!==Number(old.row_version||0))throw v18Error('Hồ sơ đã được người khác sửa. Hãy tải lại.','CONFLICT');
  const o={grade:c.grade,class:c.class_code,class_id:c.class_id,row_version:Number(old.row_version||0)+1};
  ['name','parent_phone','email','note','custom_fee','zalo_name'].forEach(k=>{if(p[k]!==undefined)o[k]=p[k];});
  if(p.enrollment_date)o.enrollment_date=p.enrollment_date;
  v17UpdateRow('Students',i+2,o);return {updated:true};
}

function v18Payments(id,m,y) {return getSheetData('Tuition').filter(t=>String(t.student_id)===String(id)&&Number(t.month)===Number(m)&&Number(t.year)===Number(y));}

function getPaymentStatus(p) {
  v18Period(p.month,p.year);
  return getStudents(p).map(s=>{const payments=v18Payments(s.student_id,p.month,p.year),paid=payments.reduce((n,t)=>n+Number(t.amount||0),0),fee=getExpectedTuitionAmount(s,getSettings().tuition_amount_default),known=v18FeeKnown(s);return Object.assign({},s,{custom_fee:fee,expected_fee:fee,paid_amount:paid,outstanding_amount:Math.max(0,fee-paid),is_paid:known?paid>=fee:payments.length>0,amount:payments.length?paid:fee,method:payments.map(t=>t.method).join(', '),paid_at:payments.map(t=>formatDateTimeSafe(t.paid_at)).join('; ')});});
}

function markAsPaid(p) {
  const period=v18Period(p.month,p.year),id=String(p.id||''),amount=Number(p.amount),method=String(p.method||'');
  if(!Number.isSafeInteger(amount)||amount<=0||!['TM','CK'].includes(method))throw v18Error('Số tiền phải là số nguyên dương; hình thức TM hoặc CK.');
  const st=getStudents({}).find(s=>String(s.student_id)===id);if(!st)throw v18Error('Không tìm thấy học sinh');
  const payments=v18Payments(id,period.month,period.year),rid=String(p.client_request_id||'');
  const repeated=rid&&payments.find(t=>String(t.client_request_id||'')===rid);if(repeated)return {tuition_id:repeated.tuition_id,student_id:id,duplicate:true};
  const paid=payments.reduce((n,t)=>n+Number(t.amount||0),0);
  if(p.expected_paid_amount!==undefined?Number(p.expected_paid_amount)!==paid:payments.length>0)throw v18Error(`Học phí tháng ${period.month}/${period.year} đã được cập nhật. Hãy tải lại.`, 'ALREADY_PAID');
  const fee=getExpectedTuitionAmount(st,getSettings().tuition_amount_default);
  if(v18FeeKnown(st)&&paid>=fee)throw v18Error('Tháng này đã hoàn tất học phí.','ALREADY_PAID');
  const tid=generateId('TUI');v17WriteObjectRow('Tuition',{tuition_id:tid,student_id:id,month:period.month,year:period.year,amount,method,paid_at:new Date(),grade:st.grade,class:st.class,class_id:st.class_id,client_request_id:rid});
  return {tuition_id:tid,student_id:id,paid_amount:paid+amount};
}

function getStudentsByClass(p) {
  const day=parseDateHelper(p.date);if(!day)throw v18Error('Ngày điểm danh không hợp lệ');
  const att=getSheetData('Attendance');
  return getStudents(p).filter(s=>{const e=parseDateHelper(s.enrollment_date||s.created_at);return !e||e.dateStr<=day.dateStr;}).map(s=>{const a=att.filter(x=>String(x.student_id)===String(s.student_id)&&parseDateHelper(x.date)&&parseDateHelper(x.date).dateStr===day.dateStr).pop();return Object.assign({},s,{status:a?a.status:'',attendance_version:Number(a&&a.row_version||0)});});
}

function saveAttendance(p) {
  const records=p.records||[],seen=new Set(),students=getStudents({}),info=v17ReadSheet('Attendance');
  const prepared=records.map(r=>{
    const st=students.find(s=>String(s.student_id)===String(r.student_id)),c=v17ResolveClass(r),day=parseDateHelper(r.date);
    if(!st||!c||!day||!['Present','AbsentExcused','AbsentUnexcused','Absent'].includes(r.status))throw v18Error('Điểm danh có học sinh/lớp/ngày/trạng thái không hợp lệ');
    if(String(st.class_id)!==String(c.class_id))throw v18Error('Học sinh không thuộc lớp đang điểm danh.');
    const key=st.student_id+'|'+day.dateStr;if(seen.has(key))throw v18Error('Một học sinh xuất hiện hai lần trong cùng buổi.');seen.add(key);
    const i=info.objects.findIndex(a=>String(a.student_id)===String(st.student_id)&&parseDateHelper(a.date)&&parseDateHelper(a.date).dateStr===day.dateStr),old=i>=0?info.objects[i]:null;
    if(r.expected_version!==undefined&&Number(r.expected_version)!==Number(old&&old.row_version||0)&&(!old||old.status!==r.status))throw v18Error('Điểm danh đã được người khác cập nhật. Hãy tải lại.','CONFLICT');
    return {r,st,c,day,i,old};
  });
  let queued=0;
  prepared.forEach(x=>{
    const {r,st,c,day,i,old}=x;if(old&&old.status===r.status)return;
    const o={student_id:st.student_id,date:day.dateStr,status:r.status,grade:c.grade,class:c.class_code,class_id:c.class_id,row_version:Number(old&&old.row_version||0)+1};
    if(old)v17UpdateRow('Attendance',i+2,o);else v17WriteObjectRow('Attendance',Object.assign({attendance_id:generateId('ATT')},o));
    if(!getSheetData('ClassSessions').some(a=>v18MatchesClass(a,c)&&parseDateHelper(a.date)&&parseDateHelper(a.date).dateStr===day.dateStr))v17WriteObjectRow('ClassSessions',{session_id:generateId('SES'),class_id:c.class_id,grade:c.grade,class:c.class_code,date:day.dateStr,created_at:new Date()});
    if(st.parent_phone||st.zalo_name){const status={Present:'CÓ MẶT',AbsentExcused:'VẮNG CÓ PHÉP',AbsentUnexcused:'VẮNG KHÔNG PHÉP',Absent:'VẮNG CHƯA PHÂN LOẠI'}[r.status],date=day.dateStr.split('-').reverse().join('/');const msg=renderMessageTemplate(r.status==='Present'?'ATTENDANCE_PRESENT':'ATTENDANCE_ABSENT',{student_name:st.name,class_name:c.class_name,date,attendance_status:status},`Học sinh ${st.name} ${status}, ngày ${date}.`);const q=queueZaloMessage(st.student_id,st.name,normalizeVietnamPhone(st.parent_phone),'ATTENDANCE','','',msg,st.zalo_name,c.class_name,c.class_id);if(!q.duplicate)queued++;}
  });return {success:true,messages_queued:queued};
}

function v18MonthReport(s,sel,data) {
  const c=v18StudentClass(s),dates=new Set(),eligible=new Set(),enroll=parseDateHelper(s.enrollment_date||s.created_at);
  const historicalClasses=data.attendance.filter(a=>{const d=parseDateHelper(a.date);return d&&d.year===sel.year&&d.month===sel.month;}).map(a=>(a.class_id&&getClassById(a.class_id))||getClassByLegacy(a.grade,a.class)||{class_id:a.class_id||v17ClassId(a.grade,a.class),grade:a.grade,class_code:a.class});const periodClasses=historicalClasses.length?historicalClasses:(c?[c]:[]);
  const add=a=>{const d=parseDateHelper(a.date);if(!d||d.year!==sel.year||d.month!==sel.month||!periodClasses.some(cls=>v18MatchesClass(a,cls)))return;dates.add(d.dateStr);if(!enroll||d.dateStr>=enroll.dateStr)eligible.add(d.dateStr);};
  data.sessions.forEach(add);data.allAttendance.forEach(add);
  const latest={};data.attendance.forEach(a=>{const d=parseDateHelper(a.date);if(d&&d.year===sel.year&&d.month===sel.month)latest[d.dateStr]=a.status;});
  const counts={Present:0,AbsentExcused:0,AbsentUnexcused:0,Absent:0};Object.keys(latest).forEach(d=>{if(Object.prototype.hasOwnProperty.call(counts,latest[d]))counts[latest[d]]++;});
  const payments=data.tuition.filter(t=>Number(t.month)===sel.month&&Number(t.year)===sel.year),paid=payments.reduce((n,t)=>n+Number(t.amount||0),0);
  const beforeEnrollment=!!enroll&&sel.key<enroll.dateStr.slice(0,7),known=v18FeeKnown(s),fee=beforeEnrollment?0:getExpectedTuitionAmount(s,getSettings().tuition_amount_default);
  return {month:sel.month,year:sel.year,key:sel.key,class_sessions:dates.size,eligible_sessions:eligible.size,present_count:counts.Present,absent_excused_count:counts.AbsentExcused,absent_unexcused_count:counts.AbsentUnexcused,absent_legacy_count:counts.Absent,expected_fee:fee,fee_known:known,is_paid:known?paid>=fee:payments.length>0,paid_amount:paid,outstanding_amount:known?Math.max(0,fee-paid):null,payment_method:payments.map(t=>t.method).join(', '),paid_at:payments.map(t=>formatDateTimeSafe(t.paid_at)).join('; '),payment_count:payments.length,before_enrollment:beforeEnrollment};
}

function getStudentMonthlyReport(p) {
  const id=String(p.student_id||p.id||''),s=getStudents({include_inactive:'true'}).find(x=>String(x.student_id)===id);if(!s)throw v18Error('Không tìm thấy học sinh');
  const now=v18NowMonth(),selection=parseMonthSelection(p.months,p.year||now.year);if(!selection.length)throw v18Error('Chưa chọn tháng báo cáo');
  if(selection.some(x=>x.key>now.key))throw v18Error('Không chọn tháng báo cáo trong tương lai.');
  const start=p.debt_from?v18Period(String(p.debt_from).split('-')[1],String(p.debt_from).split('-')[0]):selection[0];if(start.key>now.key)throw v18Error('Tháng bắt đầu công nợ phải không sau tháng hiện tại.');
  const data={allAttendance:getSheetData('Attendance'),sessions:getSheetData('ClassSessions'),tuition:getSheetData('Tuition').filter(t=>String(t.student_id)===id)};data.attendance=data.allAttendance.filter(a=>String(a.student_id)===id);
  const months=selection.map(sel=>v18MonthReport(s,sel,data)),debt=[];
  for(let y=start.year,m=start.month;y*12+m<=now.year*12+now.month;){const sel=v18Period(m,y);debt.push(v18MonthReport(s,sel,data));m++;if(m>12){m=1;y++;}}
  const unpaid=debt.filter(r=>r.outstanding_amount>0),unknown=debt.filter(r=>!r.fee_known&&!r.before_enrollment);
  return {student:Object.assign({},s,{class_info:s.class_name}),months,debt:{from:start.key,to:now.key,total_unpaid:unpaid.reduce((n,r)=>n+r.outstanding_amount,0),unpaid_months:unpaid,unknown_fee_months:unknown.map(x=>x.key),complete:unknown.length===0,fee_basis:'Mức phí hiện cấu hình; chưa có bảng học phí lịch sử theo tháng.',enrollment_basis:s.enrollment_date?'enrollment_date':s.created_at?'created_at_fallback':'unknown'}};
}

function buildStudentMonthlyReportMessage(report,flags) {
  flags=flags||{};const lines=[`Trung tâm An Phúc Education: báo cáo học sinh ${report.student.name} (${report.student.class_info})`];
  report.months.forEach(r=>{const parts=[];if(flags.sessions!==false)parts.push(`lớp học ${r.class_sessions} buổi`);if(flags.attendance!==false){parts.push(`có mặt ${r.present_count}; vắng có phép ${r.absent_excused_count}; vắng không phép ${r.absent_unexcused_count}; chưa phân loại ${r.absent_legacy_count}`);}if(flags.tuition!==false)parts.push(`đã đóng ${formatMoneyVN(r.paid_amount)} đồng${r.paid_at?' (ngày '+r.paid_at+')':''}`);lines.push(`- ${r.month}/${r.year}: ${parts.join('; ')}.`);});
  if(flags.tuition!==false){const d=report.debt;lines.push(`Công nợ từ ${d.from} đến ${d.to}: ${formatMoneyVN(d.total_unpaid)} đồng.`);d.unpaid_months.forEach(r=>lines.push(`- Chưa đủ học phí ${r.month}/${r.year}: còn ${formatMoneyVN(r.outstanding_amount)} đồng (đã đóng ${formatMoneyVN(r.paid_amount)} đồng).`));if(!d.unpaid_months.length)lines.push('Không có tháng còn thiếu học phí theo mức phí đang cấu hình.');if(!d.complete)lines.push('Chưa xác định được học phí các tháng: '+d.unknown_fee_months.join(', '));lines.push('Đối chiếu theo mức phí hiện cấu hình; cần kiểm tra nếu mức phí cũ khác.');}lines.push('Trân trọng!');return lines.join('\n');
}

function sendStudentMonthlyReport(p) {
  const report=getStudentMonthlyReport(p),s=report.student,phone=normalizeVietnamPhone(s.parent_phone),z=String(s.zalo_name||'').trim();if(!phone&&!z)throw v18Error('Chưa có liên hệ Zalo phụ huynh');
  const flags={sessions:String(p.include_sessions)!=='false',attendance:String(p.include_attendance)!=='false',tuition:String(p.include_tuition)!=='false'};if(!flags.sessions&&!flags.attendance&&!flags.tuition)throw v18Error('Chọn ít nhất một nội dung gửi.');
  const msg=buildStudentMonthlyReportMessage(report,flags),q=queueZaloMessage(s.student_id,s.name,phone,'STUDENT_MONTHLY_REPORT','','',msg,z,s.class_info,s.class_id);return {queued:q.duplicate?0:1,duplicate:q.duplicate,months:report.months.map(r=>r.month+'/'+r.year).join(', '),message_text:msg};
}

function getReportBundle(p) {
  const period=v18Period(p.month,p.year);return {statistics:getStatistics(p),lesson_report:getLessonReport(p),student_reports:getStudents(p).map(s=>getStudentMonthlyReport({student_id:s.student_id,months:[period],debt_from:p.debt_from||period.key}))};
}

function routeRequest(p,method) {
  try{
    V17_REQ_CACHE={};p=p||{};const token=PropertiesService.getScriptProperties().getProperty('SITES_API_TOKEN');
    if(!token||String(p.api_token||'')!==token)throw v18Error('Không được phép truy cập.','UNAUTHORIZED');
    ensureSchema();const action=p.action;
    const reads={getBootstrapData,getClassConfig,getMessageTemplates,getStudents,getStudentsByClass,getPaymentStatus,getMaterialStatus,getStatistics,getLessonReport,getReportBundle,getSettings,getStudentMonthlyReport};
    const writes={addStudent,updateStudent,deleteStudent,saveAttendance,markAsPaid,updateMaterialStatus,saveSettings,saveClassConfig,setClassActive,saveMessageTemplate,testZaloGroup,sendTuitionMessage,sendBulkTuitionReminders,sendClassLessonReport,sendBulkClassLessonReports,sendStudentMonthlyReport,processZaloQueue};
    let data;if(method==='GET'&&reads[action])data=reads[action](p);else if(method==='POST'&&writes[action])data=action==='processZaloQueue'?writes[action](p):withScriptLock(()=>{V17_REQ_CACHE={};return writes[action](p);});else throw v18Error('Action không tồn tại.');
    return ContentService.createTextOutput(JSON.stringify({success:true,status:'OK',data})).setMimeType(ContentService.MimeType.JSON);
  }catch(e){return ContentService.createTextOutput(JSON.stringify({success:false,status:'ERROR',message:e.message,error_code:e.code||'SERVER_ERROR',data:null})).setMimeType(ContentService.MimeType.JSON);}
}

function getStatistics(p) {
  const period=v18Period(p.month,p.year),students=getStudents(p),att=getSheetData('Attendance'),tuition=getSheetData('Tuition');
  let total_revenue=0,cash_total=0,transfer_total=0;const paid_list=[],unpaid_list=[],unpaid_two_months_list=[],absent_list=[];
  const prev=period.month===1?v18Period(12,period.year-1):v18Period(period.month-1,period.year);
  students.forEach(s=>{
    const paid=v18Payments(s.student_id,period.month,period.year),amount=paid.reduce((n,t)=>n+Number(t.amount||0),0),fee=getExpectedTuitionAmount(s,getSettings().tuition_amount_default),item=Object.assign({},s,{class_info:s.class_name,expected_fee:fee});
    paid.forEach(t=>{const n=Number(t.amount||0);total_revenue+=n;if(t.method==='TM')cash_total+=n;if(t.method==='CK')transfer_total+=n;paid_list.push(Object.assign({},item,{amount:n,method:t.method,paid_at:formatDateTimeSafe(t.paid_at)}));});
    if((v18FeeKnown(s)&&amount<fee)||(!v18FeeKnown(s)&&!paid.length)){unpaid_list.push(Object.assign({},item,{expected_fee:Math.max(0,fee-amount)}));if(!v18Payments(s.student_id,prev.month,prev.year).length)unpaid_two_months_list.push(item);}
    const daily={};att.filter(a=>String(a.student_id)===String(s.student_id)).forEach(a=>{const d=parseDateHelper(a.date);if(d&&d.year===period.year&&d.month===period.month)daily[d.dateStr]=a.status;});
    const states=Object.keys(daily).sort().reverse().map(d=>daily[d]);let n=0,cp=0,kp=0,legacy=0;for(const x of states){if(!['Absent','AbsentExcused','AbsentUnexcused'].includes(x))break;n++;if(x==='AbsentExcused')cp++;if(x==='AbsentUnexcused')kp++;if(x==='Absent')legacy++;}if(n>=2)absent_list.push(Object.assign({},item,{absent_count:n,excused_count:cp,unexcused_count:kp,legacy_count:legacy}));
  });
  return {total_students:students.length,paid_count:students.length-unpaid_list.length,unpaid_count:unpaid_list.length,total_revenue,cash_total,transfer_total,paid_list,unpaid_list,unpaid_two_months_list,absent_list,prev_month_label:`Tháng ${prev.month}/${prev.year}`};
}

function sendTuitionMessage(p) {
  const period=v18Period(p.month,p.year),s=getStudents({}).find(x=>String(x.student_id)===String(p.student_id));if(!s)throw v18Error('Không tìm thấy học sinh');
  const phone=normalizeVietnamPhone(s.parent_phone),z=String(s.zalo_name||'').trim();if(!phone&&!z)throw v18Error('Chưa có liên hệ Zalo phụ huynh');
  const payments=v18Payments(s.student_id,period.month,period.year),paid=payments.reduce((n,t)=>n+Number(t.amount||0),0),fee=getExpectedTuitionAmount(s,getSettings().tuition_amount_default);let amount,msg;
  if(p.message_type==='PAYMENT_REMINDER'){amount=Math.max(0,fee-paid);if(!v18FeeKnown(s)||amount<=0)throw v18Error('Chưa có mức học phí hoặc tháng này đã đóng đủ.');msg=buildTuitionReminderMessage(s,period.month,period.year,amount);}
  else if(p.message_type==='PAYMENT_RECEIVED'){const t=(p.tuition_id?payments.find(x=>String(x.tuition_id)===String(p.tuition_id)):payments[payments.length-1]);if(!t)throw v18Error('Chưa có giao dịch học phí để gửi biên nhận.');amount=Number(t.amount);const date=formatDateTimeSafe(t.paid_at),method=t.method==='TM'?'tiền mặt':'chuyển khoản';msg=renderMessageTemplate('PAYMENT_RECEIVED',{student_name:s.name,class_name:s.class_name,month:period.month,year:period.year,amount:formatMoneyVN(amount),date,method},`Đã ghi nhận ${formatMoneyVN(amount)} đồng của ${s.name}.`);}
  else throw v18Error('Loại tin nhắn không hợp lệ');
  const q=queueZaloMessage(s.student_id,s.name,phone,p.message_type,period.month,period.year,msg,z,s.class_name,s.class_id);return {queued:q.duplicate?0:1,duplicate:q.duplicate,amount};
}

function getMessageLogs() {return getSheetData('MessageLogs').slice(-100).reverse().map(r=>({log_id:r.log_id,name:r.name,zalo_name:r.zalo_name,phone:r.phone,status:r.status,error:r.error||'',created_at:r.created_at}));}

function getMaterialStatus(p) {const data=getSheetData('Materials');return getStudents(p).map(s=>{const r=data.find(x=>String(x.student_id)===String(s.student_id))||{},o={student_id:s.student_id,name:s.name,class_info:s.class_name};for(let b=1;b<=6;b++)o['batch'+b]=v17Bool(r['batch'+b]);return o;});}

function sendBulkTuitionReminders(p) {let queued=0,duplicate=0,skipped=0,no_contact=0,no_fee=0;getPaymentStatus(p).forEach(s=>{if(s.is_paid){skipped++;return;}if(!s.parent_phone&&!s.zalo_name){no_contact++;return;}if(!v18FeeKnown(s)||s.outstanding_amount<=0){no_fee++;return;}const result=sendTuitionMessage({student_id:s.student_id,message_type:'PAYMENT_REMINDER',month:p.month,year:p.year});if(result.duplicate)duplicate++;else queued++;});return {queued,duplicate,skipped,no_contact,no_fee};}

function parseMonthSelection(raw,fallbackYear) {
  let values=raw||[];if(typeof values==='string'){try{values=JSON.parse(values);}catch(e){values=values.split(',');}}
  if(!Array.isArray(values))throw v18Error('Danh sách tháng không hợp lệ.');const out=[],seen=new Set();
  values.forEach(item=>{let p;if(item&&typeof item==='object')p=v18Period(item.month,item.year);else{const s=String(item),a=s.match(/^(\d{4})-(\d{1,2})$/);p=a?v18Period(a[2],a[1]):v18Period(s,fallbackYear);}if(!seen.has(p.key)){seen.add(p.key);out.push(p);}});
  return out.sort((a,b)=>a.key.localeCompare(b.key));
}

function saveSettings(p) {
  if(!Object.prototype.hasOwnProperty.call(p,'tuition_amount_default'))return {updated:false};const fee=Number(p.tuition_amount_default);if(!Number.isSafeInteger(fee)||fee<0)throw v18Error('Mức phí chung phải là số nguyên >= 0.');
  const info=v17ReadSheet('Settings'),i=info.objects.findIndex(r=>r.key==='tuition_amount_default');if(i>=0)v17UpdateRow('Settings',i+2,{value:fee});else v17WriteObjectRow('Settings',{key:'tuition_amount_default',value:fee});v17Invalidate();return {updated:true};
}

function updateMaterialStatus(p) {
  const batch=Number(p.batch);if(!Number.isInteger(batch)||batch<1||batch>6)throw v18Error('Đợt tài liệu không hợp lệ');const info=v17ReadSheet('Materials'),i=info.objects.findIndex(r=>String(r.student_id)===String(p.id)),o={};o['batch'+batch]=v17Bool(p.checked);if(i>=0)v17UpdateRow('Materials',i+2,o);else v17WriteObjectRow('Materials',Object.assign({student_id:p.id},o));return {updated:true};
}

function addStudent(p) {
  const c=v17ResolveClass(p),name=String(p.name||'').trim();if(!c||!c.active)throw v18Error('Chọn một lớp đang hoạt động');if(!name)throw v18Error('Tên học sinh không được trống');if(p.enrollment_date&&!parseDateHelper(p.enrollment_date))throw v18Error('Ngày nhập học không hợp lệ');
  const id=generateId('ST');v17WriteObjectRow('Students',{student_id:id,name,grade:c.grade,class:c.class_code,class_id:c.class_id,parent_phone:p.parent_phone||'',email:p.email||'',note:p.note||'',created_at:new Date(),custom_fee:p.custom_fee??'',zalo_name:String(p.zalo_name||'').trim(),enrollment_date:p.enrollment_date||new Date(),active:true,row_version:1});return {student_id:id,class_id:c.class_id};
}
