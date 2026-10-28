// Run with: node tests/line_allocation.test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = {};
for (const id of ['number_of_lines', 'employee1', 'employee2', 'employee3', 'start1', 'start2', 'start3', 'end1', 'end2', 'end3', 'number1', 'number2', 'number3']) {
    elements[id] = {
        value: '', textContent: '', required: false, validationMessage: '',
        setCustomValidity(message) { this.validationMessage = message; }
    };
}
const context = { document: { getElementById: id => elements[id] }, console };
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/js_utils.js', 'utf8'), context);

function setup(count, ...employees) {
    elements.number_of_lines.value = String(count);
    [1, 2, 3].forEach((i, n) => { elements['employee' + i].value = employees[n] || ''; });
    context.distributeLines([60]);
}
function range(i) { return [Number(elements['start' + i].value), Number(elements['end' + i].value)]; }

setup(4, 'a', 'b');
assert.deepEqual(range(1), [1, 2]);
assert.deepEqual(range(2), [3, 4]);
assert.equal(elements.number1.textContent, '00:02:00');
assert.equal(elements.number2.textContent, '00:02:00');

setup(2, 'a', 'b');
assert.deepEqual(range(1), [1, 1]);
assert.deepEqual(range(2), [2, 2]);
assert.equal(elements.number2.textContent, '00:01:00');

setup(3, 'a', 'b', 'c');
assert.deepEqual(range(1), [1, 1]);
assert.deepEqual(range(2), [2, 2]);
assert.deepEqual(range(3), [3, 3]);

setup(2, 'a', 'b', 'c');
assert.match(elements.employee3.validationMessage, /une ligne/);
setup(4, 'a', 'a');
assert.match(elements.employee2.validationMessage, /différents/);
setup(4, 'a', '', 'c');
assert.match(elements.employee3.validationMessage, /deuxième/);

setup(4, 'a', 'b');
elements.start2.value = '2';
context.updateRangeDurations([60]);
assert.match(elements.end2.validationMessage, /chevaucher/);
elements.start2.value = '3';
context.updateRangeDurations([60]);
assert.equal(elements.end2.validationMessage, '');
assert.equal(elements.number2.textContent, '00:02:00');

setup(1, 'a');
context.updateRangeDurations([360000]);
assert.equal(elements.number1.textContent, '100:00:00');
console.log('Line allocation UI tests passed.');
