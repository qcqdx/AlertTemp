document.getElementById('confirmAddTab').addEventListener('click', function() {
    let tabName = document.getElementById('tabName').value;

    // Отправка POST-запроса для добавления вкладки в базу данных
    fetch('/add_tab', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: new URLSearchParams({
            'tab_name': tabName
        })
    })
    .then(response => response.text())
    .then(data => {
        if (data.error) {
            console.error(data.error);  // Обработка ошибки
        } else {
            location.reload();  // Перезагрузка страницы, чтобы отобразить новую вкладку
        }
    });
});

document.querySelectorAll('.deleteTabButton').forEach(function(button) {
    button.addEventListener('click', function() {
        let tabId = button.dataset.tabId;

        // Отправка POST-запроса для удаления вкладки из базы данных
        fetch('/delete_tab', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: new URLSearchParams({
                'tab_id': tabId
            })
        })
        .then(response => response.text())
        .then(data => {
            if (data.error) {
                console.error(data.error);  // Обработка ошибки
            } else {
                location.reload();  // Перезагрузка страницы, чтобы отобразить обновленный список вкладок
            }
        });
    });
});

window.addEventListener('load', function() {
    let elements = document.getElementsByTagName('INPUT');
    for (let i = 0; i < elements.length; i++) {
        elements[i].oninvalid = function(e) {
            e.target.setCustomValidity("");
            if (!e.target.validity.valid) {
                e.target.setCustomValidity("Пожалуйста, заполните это поле.");
            }
        };
        elements[i].oninput = function(e) {
            e.target.setCustomValidity("");
        };
    }

    let utcTimes = document.querySelectorAll(".utc-time");
    utcTimes.forEach(function(utcTime) {
        let utctime = utcTime.textContent;
        let utctimeJS = utctime.slice(0, 23) + 'Z';  // обрезаем лишние микросекунды и добавляем 'Z' для формата UTC
        let localTime = new Date(utctimeJS);
        utcTime.textContent = localTime.toLocaleString(undefined, { timeZone: 'Europe/Moscow' });
    });
});

window.localizationForDataTable = {
    "processing": "Подождите...",
    "search": "Поиск:",
    "lengthMenu": "Показать _MENU_ записей",
    "info": "Записи с _START_ до _END_ из _TOTAL_ записей",
    "infoEmpty": "Записи с 0 до 0 из 0 записей",
    "infoFiltered": "(отфильтровано из _MAX_ записей)",
    "infoPostFix": "",
    "loadingRecords": "Загрузка записей...",
    "zeroRecords": "Записи отсутствуют.",
    "emptyTable": "В таблице отсутствуют данные",
    "paginate": {
        "first": "Первая",
        "previous": "Предыдущая",
        "next": "Следующая",
        "last": "Последняя"
    },
    "aria": {
        "sortAscending": ": активировать для сортировки столбца по возрастанию",
        "sortDescending": ": активировать для сортировки столбца по убыванию"
    }
};

window.addEventListener('load', function() {
    $('.datatable').DataTable({
        language: window.localizationForDataTable
    });
});
