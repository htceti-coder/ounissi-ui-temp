// Example usage for tasks select:
//toggleOptgroupOptions(document.getElementById('select_id'), 'group_class');
function toggleOptgroupOptions(selectElement, optgroupClass) {
    selectElement.addEventListener('click', (event) => {
        const clickedElement = event.target;

        if (clickedElement.classList.contains(optgroupClass)) {
            const optgroup = clickedElement;
            const options = optgroup.querySelectorAll('option');

            options.forEach((option) => {
                option.selected = !option.selected;
            });
        }
    });
}
// Divide consecutive invoice lines between the selected employees.  The
// server assigns current task duration × number of lines, so the displayed
// durations use the same per-line values.
function distributeLines(list) {
    const count = Number(document.getElementById('number_of_lines').value);
    const employees = [1, 2, 3].map(i => document.getElementById('employee' + i));
    const starts = [1, 2, 3].map(i => document.getElementById('start' + i));
    const ends = [1, 2, 3].map(i => document.getElementById('end' + i));
    const labels = [1, 2, 3].map(i => document.getElementById('number' + i));

    employees.forEach(el => el.setCustomValidity(''));
    starts.concat(ends).forEach(el => { el.value = ''; el.setCustomValidity(''); el.required = false; });
    labels.forEach(el => { el.textContent = '--:--:--'; });

    if (!employees[0].value) return;
    if (employees[2].value && !employees[1].value) {
        employees[2].setCustomValidity('Sélectionnez d’abord le deuxième employé.');
        return;
    }
    const selected = employees.filter(el => el.value);
    const ids = selected.map(el => el.value);
    if (new Set(ids).size !== ids.length) {
        selected[selected.length - 1].setCustomValidity('Choisissez des employés différents.');
        return;
    }
    if (!Number.isInteger(count) || count < 1) {
        employees[0].setCustomValidity('Indiquez un nombre de lignes supérieur à zéro.');
        return;
    }
    if (selected.length > count) {
        selected[selected.length - 1].setCustomValidity('Il faut au moins une ligne par employé.');
        return;
    }

    let nextLine = 1;
    selected.forEach((employee, index) => {
        const remaining = count - nextLine + 1;
        const size = Math.ceil(remaining / (selected.length - index));
        starts[index].value = nextLine;
        ends[index].value = nextLine + size - 1;
        starts[index].required = ends[index].required = true;
        nextLine += size;
    });
    updateRangeDurations(list);
}

// Keep the estimate and validation in sync when ranges are edited manually.
function updateRangeDurations(list) {
    const count = Number(document.getElementById('number_of_lines').value);
    const ranges = [];
    [1, 2, 3].forEach(i => {
        const employee = document.getElementById('employee' + i);
        const start = document.getElementById('start' + i);
        const end = document.getElementById('end' + i);
        const label = document.getElementById('number' + i);
        start.setCustomValidity('');
        end.setCustomValidity('');
        label.textContent = '--:--:--';
        if (!employee.value) return;
        const first = Number(start.value);
        const last = Number(end.value);
        if (!start.value || !end.value || !Number.isInteger(first) || !Number.isInteger(last) || first < 1 || last < first || last > count) {
            end.setCustomValidity('Vérifiez les limites des lignes affectées.');
            return;
        }
        if (ranges.some(range => first <= range[1] && last >= range[0])) {
            end.setCustomValidity('Les plages des employés ne doivent pas se chevaucher.');
            return;
        }
        ranges.push([first, last]);
        const duration = list.length === 1
            ? Number(list[0]) * (last - first + 1)
            : sumBetweenIndexes(list, first - 1, last - 1);
        if (!Number.isFinite(duration)) {
            end.setCustomValidity('Impossible de calculer la durée pour ces lignes.');
            return;
        }
        label.textContent = secondsToHms(duration);
    });
}

	function timeStringToSeconds(timeStr) {
      const timeParts = timeStr.split(':');

      const [hours, minutes, seconds] = timeParts;

      return parseInt(hours) * 3600 + parseInt(minutes) * 60 + parseInt(seconds);
    }
    function secondsToHms(seconds) {
        seconds = Math.floor(seconds);
        const pad = (num, size) => String(num).padStart(size, "0");
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const secondsLeft = seconds % 60;

    return `${pad(hours, 2)}:${pad(minutes, 2)}:${pad(secondsLeft, 2)}`;
}

    function sumBetweenIndexes(list, lower, upper) {
      let sum = 0;
      for (let i = lower; i <= upper; i++) {
        sum += parseInt(list[i], 10);
      }
    return sum;
}
